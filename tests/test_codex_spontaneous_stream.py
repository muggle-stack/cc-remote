"""Live bridge regressions for Codex goal/automatic continuation turns."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import cc_remote.wrapper.codex_handle as codex_handle_module
import pytest
from cc_remote.codex_daemon_restart import CodexDaemonRestartState
from cc_remote.protocol import (
    Delta, Error, GoalState, ProcessEvent, SessionListInvalidated, StateEvent,
    Steer, ToolDelta, ToolResult, ToolUse, TurnBinding, TurnDiff, TurnEnd,
    TurnPlan, TurnSteered, UserMsg,
)
from cc_remote.wrapper.codex_handle import (
    CodexAppServerDisconnected, CodexHandle, CodexSpontaneousClosed,
    CodexSpontaneousOverflow, CodexSteerUserIdentityProof,
    CodexTurnStartDisconnected,
)
from cc_remote.wrapper.history_store import HistorySourceFingerprint
from tests.test_multisession import _mk_ctx, _mk_machine


class _Cfg:
    cc_cwd = "/tmp"
    tool_result_max = 8_000
    turn_reader_queue_cap = 4
    ws_max_size_bytes = 16 * 1024 * 1024


def _notification(method: str, turn_id: str, **params):
    return {
        "method": method,
        "params": {
            "threadId": "thread-spontaneous",
            "turnId": turn_id,
            **params,
        },
    }


def _context_compaction(turn_id: str, item_id: str = "context-compaction"):
    return _notification(
        "item/completed",
        turn_id,
        item={"id": item_id, "type": "contextCompaction"},
    )


def _goal_notification(
    objective: str,
    *,
    turn_id: str | None,
    status: str = "active",
    created_at: int = 1,
    updated_at: int = 1,
):
    params = {
        "threadId": "thread-spontaneous",
        "goal": {
            "threadId": "thread-spontaneous",
            "objective": objective,
            "status": status,
            "tokensUsed": 0,
            "timeUsedSeconds": 0,
            "createdAt": created_at,
            "updatedAt": updated_at,
        },
    }
    if turn_id is not None:
        params["turnId"] = turn_id
    return {"method": "thread/goal/updated", "params": params}


def test_spontaneous_bridge_is_bounded_nonblocking_and_keeps_terminal_frame():
    async def run():
        lifecycle = []

        async def on_lifecycle(phase, turn_id):
            lifecycle.append((phase, turn_id))

        handle = CodexHandle(_Cfg(), turn_lifecycle_callback=on_lifecycle)
        handle.thread_id = "thread-spontaneous"
        await asyncio.wait_for(handle._dispatch(_notification(
            "turn/started", "auto-overflow",
            turn={"id": "auto-overflow"},
        )), timeout=0.1)

        # Charge one otherwise-small frame above the bridge's byte ceiling. The
        # stdout path must return immediately instead of waiting for a consumer.
        await asyncio.wait_for(handle._dispatch(_notification(
            "item/agentMessage/delta", "auto-overflow",
            itemId="answer", delta="not retained",
        ), raw_size=8 * 1024 * 1024), timeout=0.1)
        await asyncio.wait_for(handle._dispatch(_notification(
            "turn/completed", "auto-overflow",
            turn={"id": "auto-overflow", "status": "completed"},
        )), timeout=0.1)

        items = [item async for item in
                 handle.receive_spontaneous_response("auto-overflow")]
        assert isinstance(items[0], CodexSpontaneousOverflow)
        assert items[-1]["method"] == "turn/completed"
        assert all(not (isinstance(item, dict)
                        and item.get("method") == "item/agentMessage/delta")
                   for item in items)
        assert lifecycle == [
            ("started", "auto-overflow"),
            ("completed", "auto-overflow"),
        ]

    asyncio.run(run())


def test_spontaneous_bridge_keeps_streaming_complete_items_after_gap():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = "auto-gap-recovery"
        await handle._dispatch(_notification(
            "turn/started", turn_id, turn={"id": turn_id},
        ))
        queue = handle._spontaneous_q
        stream = handle.receive_spontaneous_response(turn_id)

        await asyncio.wait_for(handle._dispatch(_notification(
            "item/agentMessage/delta", turn_id,
            itemId="lost-answer", delta="lost",
        ), raw_size=queue.max_bytes + 1), timeout=0.1)

        # Unknown deltas remain unsafe after a gap, but a subsequent explicit
        # item lifecycle must be retained behind the still-unconsumed marker.
        # Buffered stdout commonly delivers this whole sequence before the
        # independent relay-facing consumer gets its first scheduling turn.
        await handle._dispatch(_notification(
            "item/agentMessage/delta", turn_id,
            itemId="lost-answer", delta="orphan",
        ))
        fresh = [
            _notification(
                "item/started", turn_id,
                item={"id": "fresh-answer", "type": "agentMessage"},
            ),
            _notification(
                "item/agentMessage/delta", turn_id,
                itemId="fresh-answer", delta="new live detail",
            ),
            _notification(
                "item/completed", turn_id,
                item={
                    "id": "fresh-answer",
                    "type": "agentMessage",
                    "text": "new live detail",
                },
            ),
        ]
        for message in fresh:
            await handle._dispatch(message)
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "completed"},
        ))

        rest = [item async for item in stream]
        assert isinstance(rest[0], CodexSpontaneousOverflow)
        assert rest[1:-1] == fresh
        assert rest[-1]["method"] == "turn/completed"
        assert all(
            (item.get("params") or {}).get("delta") != "orphan"
            for item in rest
            if isinstance(item, dict)
        )

    asyncio.run(run())


def test_spontaneous_bridge_completion_snapshot_and_terminal_win_after_gap():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = "auto-completion-race"
        await handle._dispatch(_notification(
            "turn/started", turn_id, turn={"id": turn_id},
        ))
        queue = handle._spontaneous_q

        await handle._dispatch(_notification(
            "item/agentMessage/delta", turn_id,
            itemId="lost", delta="lost",
        ), raw_size=queue.max_bytes + 1)

        completion = _notification(
            "item/completed", turn_id,
            item={
                "id": "completion-only-answer",
                "type": "agentMessage",
                "text": "final snapshot",
            },
        )
        await handle._dispatch(completion)
        # EOF/close may race the final app-server frame. The terminal must replace
        # that provisional close without erasing the retained gap or completion.
        handle._close_spontaneous_stream(turn_id)
        terminal = _notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        )
        await handle._dispatch(terminal, raw_size=queue.max_bytes + 1)
        # Duplicate terminal and a later close must not add another end frame.
        await handle._dispatch(terminal, raw_size=queue.max_bytes + 1)
        handle._close_spontaneous_stream(turn_id)

        frames = [
            item async for item in
            handle.receive_spontaneous_response(turn_id)
        ]
        assert isinstance(frames[0], CodexSpontaneousOverflow)
        assert frames[1] == completion
        assert frames[2]["method"] == "turn/completed"
        assert frames[2]["params"]["turn"]["status"] == "interrupted"
        assert len(frames) == 3
        assert not any(
            isinstance(item, CodexSpontaneousClosed) for item in frames)

    asyncio.run(run())


def test_compaction_interrupted_boundary_keeps_same_turn_stream_open():
    async def run():
        lifecycle = []

        async def on_lifecycle(phase, turn_id):
            lifecycle.append((phase, turn_id))

        handle = CodexHandle(_Cfg(), turn_lifecycle_callback=on_lifecycle)
        handle.thread_id = "thread-spontaneous"
        turn_id = "compact-continuation"
        started = _notification(
            "turn/started", turn_id, turn={"id": turn_id},
        )
        await handle._dispatch(started)
        compacted = _notification("thread/compacted", turn_id)
        await handle._dispatch(compacted)
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        ))

        assert handle.turn_active is True
        assert handle.turn_id == turn_id
        assert handle._spontaneous_turn_id == turn_id
        assert lifecycle == [("started", turn_id)]

        continuation = [
            _notification(
                "item/started", turn_id,
                item={"id": "after-compact", "type": "agentMessage"},
            ),
            _notification(
                "item/agentMessage/delta", turn_id,
                itemId="after-compact", delta="continued",
            ),
            _notification(
                "item/completed", turn_id,
                item={
                    "id": "after-compact",
                    "type": "agentMessage",
                    "text": "continued",
                },
            ),
        ]
        for message in continuation:
            await handle._dispatch(message)
        final = _notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "completed"},
        )
        await handle._dispatch(final)

        frames = [
            item async for item in
            handle.receive_spontaneous_response(turn_id)
        ]
        assert frames == [started, compacted, *continuation, final]
        assert lifecycle == [
            ("started", turn_id),
            ("completed", turn_id),
        ]
        assert handle.turn_active is False
        assert handle.turn_id is None

    asyncio.run(run())


def test_managed_compaction_interrupted_boundary_keeps_response_open():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = "managed-compact-continuation"
        handle.turn_id = turn_id
        handle.turn_active = True
        handle._open_managed_stream()

        compacted = _notification("thread/compacted", turn_id)
        await handle._dispatch(compacted)
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        ))
        assert handle.turn_active is True
        assert handle.turn_id == turn_id

        continuation = _notification(
            "item/completed", turn_id,
            item={
                "id": "managed-after-compact",
                "type": "agentMessage",
                "text": "continued",
            },
        )
        await handle._dispatch(continuation)
        final = _notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        )
        await handle._dispatch(final)

        frames = [item async for item in handle.receive_response()]
        assert frames == [compacted, continuation, final]
        assert frames[-1]["params"]["turn"]["status"] == "interrupted"
        assert handle.turn_active is False
        assert handle.turn_id is None

    asyncio.run(run())


def test_official_context_compaction_item_arms_managed_continuation():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = "managed-official-context-compaction"
        handle.turn_id = turn_id
        handle.turn_active = True
        handle._open_managed_stream()

        compacted = _context_compaction(turn_id)
        await handle._dispatch(compacted)
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        ))

        fence = handle._managed_compaction_continuation
        assert fence is not None
        assert fence.awaiting_replacement is True
        assert handle.turn_active is True

        continuation = _notification(
            "item/completed", turn_id,
            item={
                "id": "official-after-compaction",
                "type": "agentMessage",
                "text": "continued",
            },
        )
        await handle._dispatch(continuation)
        final = _notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "completed"},
        )
        await handle._dispatch(final)

        assert [item async for item in handle.receive_response()] == [
            compacted, continuation, final,
        ]

    asyncio.run(run())


def test_same_native_turn_steer_item_confirms_compaction_continuation():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = "managed-compact-steered"
        handle.turn_id = turn_id
        handle.turn_active = True
        handle._open_managed_stream()

        compacted = _notification("thread/compacted", turn_id)
        await handle._dispatch(compacted)
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        ))

        steered_user = _notification(
            "item/completed", turn_id,
            item={
                "id": "same-turn-steer",
                "type": "userMessage",
                "text": "guide the still-running turn",
            },
        )
        await handle._dispatch(steered_user)

        fence = handle._managed_compaction_continuation
        assert fence is not None
        assert fence.awaiting_replacement is False
        assert fence.suppressed_terminal is None
        assert handle.turn_active is True
        assert handle.turn_id == turn_id

        final = _notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "completed"},
        )
        await handle._dispatch(final)
        frames = [item async for item in handle.receive_response()]
        assert frames == [compacted, steered_user, final]

    asyncio.run(run())


def test_repeated_compact_does_not_discard_suppressed_terminal():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = "managed-repeated-compact"
        handle.turn_id = turn_id
        handle.turn_active = True
        handle._open_managed_stream()

        first_compact = _notification("thread/compacted", turn_id)
        terminal = _notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        )
        repeated_compact = _notification("thread/compacted", turn_id)
        await handle._dispatch(first_compact)
        await handle._dispatch(terminal)
        fence = handle._managed_compaction_continuation
        assert fence is not None
        assert fence.suppressed_terminal == terminal

        await handle._dispatch(repeated_compact)
        assert handle._managed_compaction_continuation is fence
        assert fence.awaiting_replacement is True
        assert fence.suppressed_terminal == terminal

        assert await handle._release_managed_compaction_continuation() is True
        frames = [item async for item in handle.receive_response()]
        assert frames == [first_compact, repeated_compact, terminal]

    asyncio.run(run())


def test_managed_compaction_can_continue_under_a_new_native_turn_id():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        logical_turn_id = "managed-before-compact"
        replacement_turn_id = "managed-after-compact"
        handle.turn_id = logical_turn_id
        handle.turn_active = True
        handle.remember_owned_turn_id(logical_turn_id)
        handle._open_managed_stream()

        compacted = _notification("thread/compacted", logical_turn_id)
        await handle._dispatch(compacted)
        await handle._dispatch(_notification(
            "turn/completed", logical_turn_id,
            turn={"id": logical_turn_id, "status": "interrupted"},
        ))

        replacement_started = _notification(
            "turn/started", replacement_turn_id,
            turn={"id": replacement_turn_id},
        )
        replacement_answer = _notification(
            "item/completed", replacement_turn_id,
            item={
                "id": "replacement-answer",
                "type": "agentMessage",
                "text": "continued under a replacement native id",
            },
        )
        replacement_completed = _notification(
            "turn/completed", replacement_turn_id,
            turn={"id": replacement_turn_id, "status": "completed"},
        )
        await handle._dispatch(replacement_started)
        assert handle.turn_id == logical_turn_id
        assert replacement_turn_id not in handle.owned_turn_ids
        fence = handle._managed_compaction_continuation
        assert fence is not None
        assert fence.candidate_turn_id == replacement_turn_id
        await handle._dispatch(replacement_answer)
        assert handle.turn_id == replacement_turn_id
        assert replacement_turn_id in handle.owned_turn_ids
        await handle._dispatch(replacement_completed)

        frames = [item async for item in handle.receive_response()]
        assert [frame["method"] for frame in frames] == [
            "thread/compacted",
            "turn/started",
            "item/completed",
            "turn/completed",
        ]
        # The handle keeps the real native id for interrupt/ownership, while
        # the managed consumer sees one logical turn from start to terminal.
        assert all(
            frame["params"].get("turnId") == logical_turn_id
            for frame in frames[1:]
        )
        assert frames[1]["params"]["turn"]["id"] == logical_turn_id
        assert frames[-1]["params"]["turn"]["id"] == logical_turn_id
        assert handle.turn_active is False
        assert handle.turn_id is None

    asyncio.run(run())


def test_foreign_user_and_goal_notifications_do_not_release_compaction_fence():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = "managed-compact-owned"
        handle.turn_id = turn_id
        handle.turn_active = True
        handle._open_managed_stream()
        await handle._dispatch(_notification("thread/compacted", turn_id))
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        ))
        fence = handle._managed_compaction_continuation
        assert fence is not None
        assert fence.suppressed_terminal is not None

        foreign_user = _notification(
            "item/completed", turn_id,
            item={"id": "foreign-user", "type": "userMessage", "text": "hi"},
        )
        foreign_user["params"]["threadId"] = "thread-sibling"
        await handle._dispatch(foreign_user)
        foreign_goal = _goal_notification(
            "foreign goal", turn_id=turn_id,
        )
        foreign_goal["params"]["threadId"] = "thread-sibling"
        foreign_goal["params"]["goal"]["threadId"] = "thread-sibling"
        await handle._dispatch(foreign_goal)

        assert handle._managed_compaction_continuation is fence
        assert fence.suppressed_terminal is not None
        assert handle.turn_active is True
        assert handle.turn_id == turn_id
        handle._discard_managed_compaction_continuation()

    asyncio.run(run())


def test_foreign_turn_keeps_only_registered_remote_steer_identity():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        handle.turn_id = "remote-expected-turn"
        handle.turn_active = True
        handle._using_daemon_proxy = True
        handle.remember_owned_turn_id("remote-expected-turn")
        handle._open_managed_stream()
        handle._register_steer_user_identity(
            "remote-client-message",
            "thread-spontaneous",
            "remote-expected-turn",
        )

        delayed = _notification(
            "item/started",
            "foreign-cli-turn",
            item={
                "id": "native-delayed-user",
                "clientId": "remote-client-message",
                "type": "userMessage",
                "content": [{"type": "text", "text": "private prompt"}],
            },
        )
        await handle._dispatch(delayed)
        completed = {
            **delayed,
            "method": "item/completed",
        }
        await handle._dispatch(completed)
        assert handle._turn_q.qsize() == 0

        # An exact RPC acceptance releases only the content-free identity proof.
        # The foreign userMessage frame itself must remain outside the stream.
        handle._accept_steer_user_identity("remote-client-message")
        proof = await asyncio.wait_for(handle._turn_q.get(), timeout=0.1)
        assert isinstance(proof, CodexSteerUserIdentityProof)
        assert (
            proof.thread_id,
            proof.expected_turn_id,
            proof.native_turn_id,
            proof.native_message_id,
            proof.client_message_id,
        ) == (
            "thread-spontaneous",
            "remote-expected-turn",
            "foreign-cli-turn",
            "native-delayed-user",
            "remote-client-message",
        )
        assert handle._turn_q.qsize() == 0

        handle._register_steer_user_identity(
            "post-response-message",
            "thread-spontaneous",
            "remote-expected-turn",
        )
        handle._accept_steer_user_identity("post-response-message")
        post_response = _notification(
            "item/started",
            "foreign-cli-turn",
            item={
                "id": "post-response-native-user",
                "clientId": "post-response-message",
                "type": "userMessage",
                "content": [{"type": "text", "text": "private"}],
            },
        )
        await handle._dispatch(post_response)
        assert isinstance(
            await asyncio.wait_for(handle._turn_q.get(), timeout=0.1),
            CodexSteerUserIdentityProof,
        )
        post_response["method"] = "item/completed"
        await handle._dispatch(post_response)
        assert "post-response-message" not in (
            handle._pending_steer_user_identities)
        assert handle._turn_q.qsize() == 0

        handle._register_steer_user_identity(
            "known-other-message",
            "thread-spontaneous",
            "remote-expected-turn",
        )
        sibling = _notification(
            "item/completed",
            "foreign-cli-turn",
            item={
                "id": "sibling-user",
                "clientId": "known-other-message",
                "type": "userMessage",
                "content": [{"type": "text", "text": "sibling"}],
            },
        )
        sibling["params"]["threadId"] = "thread-sibling"
        await handle._dispatch(sibling)
        handle._accept_steer_user_identity("known-other-message")
        assert handle._turn_q.qsize() == 0

        unknown = _notification(
            "item/completed",
            "foreign-cli-turn",
            item={
                "id": "unknown-user",
                "clientId": "unknown-client-message",
                "type": "userMessage",
                "content": [{"type": "text", "text": "unknown"}],
            },
        )
        await handle._dispatch(unknown)
        assert handle._turn_q.qsize() == 0

    asyncio.run(run())


def test_turn_start_binds_exact_client_user_to_split_stream_task():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.proc = SimpleNamespace(returncode=None)
        handle.thread_id = "thread-spontaneous"
        requests = []

        async def send(request):
            requests.append(request)
            await handle._dispatch(_notification(
                "item/started",
                "rollout-task",
                item={
                    "id": "native-query-user",
                    "clientId": "browser-query-message",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "private"}],
                },
            ))
            await handle._dispatch(_notification(
                "item/agentMessage/delta",
                "rollout-task",
                itemId="answer",
                delta="working",
            ))
            await handle._dispatch({
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"turn": {"id": "control-turn"}},
            })

        handle._send = send
        result = await handle.query(
            "private",
            client_user_message_id="browser-query-message",
        )

        assert result == "control-turn"
        assert requests[0]["params"]["clientUserMessageId"] == (
            "browser-query-message")
        proof = await asyncio.wait_for(handle._turn_q.get(), timeout=0.1)
        assert isinstance(proof, CodexSteerUserIdentityProof)
        assert proof.kind == "query"
        assert (
            proof.expected_turn_id,
            proof.native_turn_id,
            proof.native_message_id,
            proof.client_message_id,
        ) == (
            "control-turn",
            "rollout-task",
            "native-query-user",
            "browser-query-message",
        )
        streamed = await asyncio.wait_for(handle._turn_q.get(), timeout=0.1)
        assert streamed["params"]["turnId"] == "control-turn"
        assert handle.owned_turn_ids.issuperset({
            "control-turn", "rollout-task",
        })

    asyncio.run(run())


@pytest.mark.parametrize("with_exact_user_proof", [False, True])
def test_turn_start_disconnect_never_blindly_replays_submission(
        with_exact_user_proof):
    async def run():
        handle = CodexHandle(_Cfg(), daemon_mode="auto")
        handle.proc = SimpleNamespace(returncode=None)
        handle.thread_id = "thread-spontaneous"
        handle.app_server_version = "0.150.0"
        handle._daemon_proxy_established = True
        requests = []

        async def send(request):
            requests.append(request)
            if with_exact_user_proof:
                await handle._dispatch(_notification(
                    "item/started",
                    "native-task",
                    item={
                        "id": "native-query-user",
                        "clientId": "browser-query-message",
                        "type": "userMessage",
                        "content": [
                            {"type": "text", "text": "private"},
                        ],
                    },
                ))
            handle._pending[request["id"]].set_exception(
                CodexAppServerDisconnected("0.150.0", 7))

        handle._send = send
        with pytest.raises(CodexTurnStartDisconnected) as raised:
            await handle.query(
                "do one non-idempotent task",
                client_user_message_id="browser-query-message",
            )

        error = raised.value
        assert len(requests) == 1
        assert error.accepted is with_exact_user_proof
        assert error.app_server_version == "0.150.0"
        assert error.generation == 7
        assert error.client_message_id == "browser-query-message"
        assert error.native_turn_id == (
            "native-task" if with_exact_user_proof else None)
        assert error.native_message_id == (
            "native-query-user" if with_exact_user_proof else None)
        assert handle.turn_active is False
        assert handle.turn_start_pending is False
        assert handle._turn_q is None

    asyncio.run(run())


def test_turn_start_reconciliation_uses_exact_user_identity_and_liveness():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        requests = []

        async def request(method, params=None):
            requests.append((method, params))
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thread-spontaneous",
                        "status": {"type": "active", "activeFlags": []},
                    },
                }
            assert method == "thread/turns/list"
            return {
                "data": [{
                    "id": "control-turn",
                    "status": "inProgress",
                    "items": [{
                        "id": "native-user",
                        "clientId": "browser-query-message",
                        "type": "userMessage",
                    }],
                }],
                "nextCursor": None,
            }

        handle._request = request
        probe = await handle.reconcile_turn_start(
            "browser-query-message",
            native_turn_id="native-task",
            native_message_id="native-user",
        )

        assert probe.thread_active is True
        assert probe.active_turn_id == "control-turn"
        assert probe.matched_turn_id == "control-turn"
        assert probe.matched_turn_active is True
        assert requests == [
            ("thread/read", {
                "threadId": "thread-spontaneous",
                "includeTurns": False,
            }),
            ("thread/turns/list", {
                "threadId": "thread-spontaneous",
                "cursor": None,
                "limit": 8,
                "sortDirection": "desc",
                "itemsView": "summary",
            }),
        ]

    asyncio.run(run())


def test_turn_start_reconciliation_does_not_claim_unmatched_active_turn():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"

        async def request(method, _params=None):
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thread-spontaneous",
                        "status": {"type": "active", "activeFlags": []},
                    },
                }
            return {
                "data": [{
                    "id": "foreign-active-turn",
                    "status": "inProgress",
                    "items": [{
                        "id": "foreign-user",
                        "clientId": "another-client",
                        "type": "userMessage",
                    }],
                }],
            }

        handle._request = request
        probe = await handle.reconcile_turn_start("browser-query-message")

        assert probe.thread_active is True
        assert probe.active_turn_id == "foreign-active-turn"
        assert probe.matched_turn_id is None
        assert probe.matched_turn_active is False

    asyncio.run(run())


def test_turn_start_reconciliation_prefers_newer_active_turn_page_over_idle_read():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"

        async def request(method, _params=None):
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thread-spontaneous",
                        "status": {"type": "idle"},
                    },
                }
            return {
                "data": [{
                    "id": "new-active-turn",
                    "status": "inProgress",
                    "items": [{
                        "id": "native-user",
                        "clientId": "browser-query-message",
                        "type": "userMessage",
                    }],
                }],
                "nextCursor": None,
            }

        handle._request = request
        probe = await handle.reconcile_turn_start(
            "browser-query-message",
        )

        assert probe.thread_active is True
        assert probe.active_turn_id == "new-active-turn"
        assert probe.latest_turn_id == "new-active-turn"
        assert probe.matched_turn_id == "new-active-turn"
        assert probe.matched_turn_active is True
        assert probe.matched_turn_is_latest is True
        assert probe.newer_turns_present is False

    asyncio.run(run())


def test_turn_start_reconciliation_reports_completed_successor_ordering():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"

        async def request(method, _params=None):
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thread-spontaneous",
                        "status": {"type": "idle"},
                    },
                }
            return {
                "data": [
                    {
                        "id": "completed-successor",
                        "status": "completed",
                        "items": [],
                    },
                    {
                        "id": "submitted-turn",
                        "status": "completed",
                        "items": [{
                            "id": "native-user",
                            "clientId": "browser-query-message",
                            "type": "userMessage",
                        }],
                    },
                ],
                "nextCursor": None,
            }

        handle._request = request
        probe = await handle.reconcile_turn_start(
            "browser-query-message",
        )

        assert probe.thread_active is False
        assert probe.latest_turn_id == "completed-successor"
        assert probe.matched_turn_id == "submitted-turn"
        assert probe.matched_turn_is_latest is False
        assert probe.newer_turns_present is True
        assert probe.page_truncated is False

    asyncio.run(run())


def test_turn_start_reconciliation_marks_bounded_page_truncation():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"

        async def request(method, _params=None):
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thread-spontaneous",
                        "status": {"type": "idle"},
                    },
                }
            return {
                "data": [{
                    "id": f"newer-{index}",
                    "status": "completed",
                    "items": [],
                } for index in range(8)],
                "nextCursor": "older-page",
            }

        handle._request = request
        probe = await handle.reconcile_turn_start(
            "browser-query-message",
            native_turn_id="submitted-turn",
            native_message_id="native-user",
        )

        assert probe.thread_active is False
        assert probe.latest_turn_id == "newer-0"
        assert probe.matched_turn_id is None
        assert probe.page_truncated is True

    asyncio.run(run())


def test_machine_persists_cross_turn_steer_identity_and_refreshes_history(
    tmp_path,
):
    async def run():
        machine, _transport = _mk_machine()
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text("{}\n", encoding="utf-8")
        machine._codex_rollout_for_wire = lambda _sid: str(rollout)
        refreshes = []
        machine._schedule_history_refresh = lambda *args, **kwargs: (
            refreshes.append((args, kwargs)))

        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.turn_id = "remote-expected-turn"
        handle.turn_active = True
        handle._daemon_proxy_established = True
        handle.remember_owned_turn_id("remote-expected-turn")
        ctx.sdk = handle
        ctx.codex_daemon_epoch = "unmarked"
        ctx.codex_published_steers["remote-client-message"] = (
            "remote-expected-turn")
        machine.sessions[ctx.key] = ctx
        machine._claim_codex_turn(
            ctx, "remote-expected-turn", "remote-client-message")
        revision = machine._history_revision(ctx.session_id)

        proof = CodexSteerUserIdentityProof(
            thread_id=ctx.session_id,
            expected_turn_id="remote-expected-turn",
            native_turn_id="foreign-cli-turn",
            native_message_id="native-delayed-user",
            client_message_id="remote-client-message",
            generation=handle._generation,
        )
        assert await machine._apply_codex_steer_user_identity(ctx, proof)
        aliases = machine._codex_client_messages.get(rollout)
        assert aliases.native_messages == {
            "native-delayed-user": "remote-client-message",
        }
        lease = machine._codex_turn_leases.get(ctx.session_id)
        assert lease is not None
        source = rollout.stat()
        assert lease.stream_task_ids(
            source_device=source.st_dev,
            source_inode=source.st_ino,
        ) == {"foreign-cli-turn"}
        assert handle.owned_turn_ids.issuperset({
            "remote-expected-turn", "foreign-cli-turn",
        })
        assert machine._history_revision(ctx.session_id) != revision
        assert len(refreshes) == 1
        assert refreshes[0][1]["before"] is None
        assert refreshes[0][1]["limit"] == machine.MIRROR_LIMIT
        assert refreshes[0][1]["detail"] == "summary"
        assert ctx.codex_published_steers == {
            "remote-client-message": "remote-expected-turn",
        }
        assert ctx.codex_pending_steer_user_identities == {}

        unknown = CodexSteerUserIdentityProof(
            thread_id=ctx.session_id,
            expected_turn_id="unknown-turn",
            native_turn_id="foreign-cli-turn",
            native_message_id="unknown-native-user",
            client_message_id="unknown-client-message",
            generation=handle._generation,
        )
        assert not await machine._apply_codex_steer_user_identity(ctx, unknown)
        assert machine._codex_client_messages.get(rollout).native_messages == {
            "native-delayed-user": "remote-client-message",
        }

    asyncio.run(run())


def test_machine_persists_split_initial_query_identity(tmp_path):
    async def run():
        machine, _transport = _mk_machine()
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text("{}\n", encoding="utf-8")
        machine._codex_rollout_for_wire = lambda _sid: str(rollout)
        machine._schedule_history_refresh = lambda *args, **kwargs: None

        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.turn_id = "control-turn"
        handle.turn_active = True
        handle._daemon_proxy_established = True
        handle.remember_owned_turn_id("control-turn")
        ctx.sdk = handle
        ctx.codex_daemon_epoch = "unmarked"
        machine.sessions[ctx.key] = ctx
        machine._claim_codex_turn(
            ctx, "control-turn", "browser-query-message")

        proof = CodexSteerUserIdentityProof(
            thread_id=ctx.session_id,
            expected_turn_id="control-turn",
            native_turn_id="rollout-task",
            native_message_id="native-query-user",
            client_message_id="browser-query-message",
            generation=handle._generation,
            kind="query",
        )
        assert await machine._apply_codex_steer_user_identity(ctx, proof)

        aliases = machine._codex_client_messages.get(rollout)
        assert aliases.native_messages == {
            "native-query-user": "browser-query-message",
        }
        lease = machine._codex_turn_leases.get(ctx.session_id)
        assert lease is not None
        source = rollout.stat()
        assert lease.stream_task_ids(
            source_device=source.st_dev,
            source_inode=source.st_ino,
        ) == {"rollout-task"}

    asyncio.run(run())


def test_cross_turn_identity_retries_when_rollout_path_appears_late(tmp_path):
    async def run():
        machine, _transport = _mk_machine()
        rollout = tmp_path / "late-rollout.jsonl"
        machine._codex_rollout_for_wire = lambda _sid: None
        machine._schedule_history_refresh = lambda *args, **kwargs: None
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        ctx.sdk = handle
        ctx.codex_published_steers["remote-client-message"] = (
            "remote-expected-turn")
        machine.sessions[ctx.key] = ctx
        proof = CodexSteerUserIdentityProof(
            thread_id=ctx.session_id,
            expected_turn_id="remote-expected-turn",
            native_turn_id="foreign-cli-turn",
            native_message_id="native-delayed-user",
            client_message_id="remote-client-message",
            generation=handle._generation,
        )

        assert await machine._apply_codex_steer_user_identity(ctx, proof)
        assert ctx.codex_pending_steer_user_identities == {
            "native-delayed-user": proof,
        }
        rollout.write_text("{}\n", encoding="utf-8")
        machine._codex_rollout_for_wire = lambda _sid: str(rollout)
        await machine._cleanup_codex_steer_attachments(ctx)

        assert ctx.codex_pending_steer_user_identities == {}
        assert machine._codex_client_messages.get(rollout).native_messages == {
            "native-delayed-user": "remote-client-message",
        }

    asyncio.run(run())


@pytest.mark.parametrize("persisted_first", [False, True])
def test_repeated_codex_identity_does_not_block_idle_update(tmp_path, persisted_first):
    async def run():
        machine, _transport = _mk_machine()
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text("{}\n", encoding="utf-8")
        machine._codex_rollout_for_wire = lambda _sid: str(rollout)
        machine._schedule_history_refresh = lambda *args, **kwargs: None
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        ctx.sdk = handle
        ctx.codex_published_steers["remote-client-message"] = "control-turn"
        machine.sessions[ctx.key] = ctx
        proof = CodexSteerUserIdentityProof(
            thread_id=ctx.session_id,
            expected_turn_id="control-turn",
            native_turn_id="native-turn",
            native_message_id="native-user",
            client_message_id="remote-client-message",
            generation=handle._generation,
        )
        if persisted_first:
            machine._codex_client_messages.put(
                rollout, "native-turn", "remote-client-message",
                native_message_id="native-user")

        assert await machine._apply_codex_steer_user_identity(ctx, proof)
        assert await machine._apply_codex_steer_user_identity(ctx, proof)
        await machine._cleanup_codex_steer_attachments(ctx)

        assert ctx.codex_pending_steer_user_identities == {}
        assert machine._ui_updates.busy_count() == 0
        assert machine._codex_client_messages.get(rollout).native_messages == {
            "native-user": "remote-client-message",
        }
        await machine._command_scheduler.close()

    asyncio.run(run())


def test_retained_initial_codex_identity_settles_after_owner_released(tmp_path):
    async def run():
        machine, _transport = _mk_machine()
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text("{}\n", encoding="utf-8")
        machine._codex_rollout_for_wire = lambda _sid: str(rollout)
        machine._schedule_history_refresh = lambda *args, **kwargs: None
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        proof = CodexSteerUserIdentityProof(
            thread_id=ctx.session_id,
            expected_turn_id="control-turn",
            native_turn_id="native-turn",
            native_message_id="native-user",
            client_message_id="remote-client-message",
            generation=handle._generation,
            kind="query",
        )
        ctx.codex_pending_steer_user_identities["native-user"] = proof
        machine._codex_client_messages.put(
            rollout, "native-turn", "remote-client-message",
            native_message_id="native-user")
        assert machine._ui_updates.busy_count() == 1

        await machine._cleanup_codex_steer_attachments(ctx)

        assert ctx.codex_pending_steer_user_identities == {}
        assert machine._ui_updates.busy_count() == 0
        await machine._command_scheduler.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["write", "conflict", "generation"])
def test_unsettled_codex_identity_keeps_update_blocked(tmp_path, failure, monkeypatch):
    async def run():
        machine, _transport = _mk_machine()
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text("{}\n", encoding="utf-8")
        machine._codex_rollout_for_wire = lambda _sid: str(rollout)
        machine._schedule_history_refresh = lambda *args, **kwargs: None
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        proof = CodexSteerUserIdentityProof(
            thread_id=ctx.session_id,
            expected_turn_id="control-turn",
            native_turn_id="native-turn",
            native_message_id="native-user",
            client_message_id="remote-client-message",
            generation=handle._generation,
            kind="query",
        )
        ctx.codex_pending_steer_user_identities["native-user"] = proof
        if failure == "write":
            def fail(*args, **kwargs):
                raise OSError("read-only fixture")
            monkeypatch.setattr(machine._codex_client_messages, "put", fail)
        elif failure == "conflict":
            machine._codex_client_messages.put(
                rollout, "native-turn", "another-message",
                native_message_id="native-user")
        else:
            handle._generation += 1

        await machine._cleanup_codex_steer_attachments(ctx)

        assert ctx.codex_pending_steer_user_identities == {"native-user": proof}
        assert machine._ui_updates.busy_count() == 1
        await machine._command_scheduler.close()

    asyncio.run(run())


def test_history_settles_already_persisted_pending_codex_identity(tmp_path):
    async def run():
        machine, _transport = _mk_machine()
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text("{}\n", encoding="utf-8")
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        ctx.sdk = CodexHandle(machine.cfg)
        ctx.sdk.thread_id = ctx.session_id
        machine.sessions[ctx.key] = ctx
        proof = CodexSteerUserIdentityProof(
            thread_id=ctx.session_id,
            expected_turn_id="control-turn",
            native_turn_id="native-turn",
            native_message_id="native-user",
            client_message_id="remote-client-message",
            generation=ctx.sdk._generation,
        )
        ctx.codex_pending_steer_user_identities["native-user"] = proof
        machine._codex_client_messages.put(
            rollout, "native-turn", "remote-client-message",
            native_message_id="native-user")
        revision = machine._history_revision(ctx.key)

        await machine._backfill_official_codex_client_message_ids(
            ctx, ctx.key, str(rollout), HistorySourceFingerprint.capture(rollout),
            {"remote-client-message"},
            (("native-turn", "native-user", "remote-client-message"),),
        )

        assert ctx.codex_pending_steer_user_identities == {}
        assert machine._ui_updates.busy_count() == 0
        assert machine._history_revision(ctx.key) == revision
        await machine._command_scheduler.close()

    asyncio.run(run())


def test_official_history_backfills_known_cross_turn_alias_while_cold(tmp_path):
    async def run():
        machine, _transport = _mk_machine()
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text("{}\n", encoding="utf-8")
        machine._codex_client_messages.put(
            rollout,
            "remote-expected-turn",
            "remote-client-message",
            segment_index=0,
        )
        source = HistorySourceFingerprint.capture(rollout)
        revision = machine._history_revision("thread-cold")

        await machine._backfill_official_codex_client_message_ids(
            None,
            "thread-cold",
            str(rollout),
            source,
            {"remote-client-message"},
            ((
                "foreign-cli-turn",
                "native-delayed-user",
                "remote-client-message",
            ),),
        )

        aliases = machine._codex_client_messages.get(rollout)
        assert aliases.segments == {
            ("remote-expected-turn", 0): "remote-client-message",
        }
        assert aliases.native_messages == {
            "native-delayed-user": "remote-client-message",
        }
        assert machine._history_revision("thread-cold") != revision

    asyncio.run(run())


def test_official_history_adds_second_native_alias_for_known_message(tmp_path):
    async def run():
        machine, _transport = _mk_machine()
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text("{}\n", encoding="utf-8")
        machine._codex_client_messages.put(
            rollout,
            "native-turn",
            "remote-client-message",
            segment_index=0,
            native_message_id="live-native-user",
        )
        source = HistorySourceFingerprint.capture(rollout)
        revision = machine._history_revision("thread-cold")

        await machine._backfill_official_codex_client_message_ids(
            None,
            "thread-cold",
            str(rollout),
            source,
            {"remote-client-message"},
            ((
                "native-turn",
                "msg_official_native_user",
                "remote-client-message",
            ),),
        )

        aliases = machine._codex_client_messages.get(rollout)
        assert aliases.segments == {
            ("native-turn", 0): "remote-client-message",
        }
        assert aliases.native_messages == {
            "live-native-user": "remote-client-message",
            "msg_official_native_user": "remote-client-message",
        }
        assert machine._history_revision("thread-cold") != revision

        repeated_revision = machine._history_revision("thread-cold")
        await machine._backfill_official_codex_client_message_ids(
            None,
            "thread-cold",
            str(rollout),
            source,
            {"remote-client-message"},
            ((
                "native-turn",
                "msg_official_native_user",
                "remote-client-message",
            ),),
        )
        assert machine._history_revision("thread-cold") == repeated_revision

    asyncio.run(run())


def test_active_history_adds_official_alias_without_replacing_live_id(tmp_path):
    async def run():
        machine, _transport = _mk_machine()
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text("{}\n", encoding="utf-8")
        machine._codex_client_messages.put(
            rollout,
            "native-turn",
            "remote-client-message",
            segment_index=0,
            native_message_id="live-native-user",
        )
        ctx = _mk_ctx("thread-active", "thread-active")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        source = HistorySourceFingerprint.capture(rollout)

        await machine._backfill_official_codex_client_message_ids(
            ctx,
            ctx.session_id,
            str(rollout),
            source,
            {"remote-client-message"},
            ((
                "native-turn",
                "msg_official_native_user",
                "remote-client-message",
            ),),
        )

        aliases = machine._codex_client_messages.get(rollout)
        assert aliases.native_messages == {
            "live-native-user": "remote-client-message",
            "msg_official_native_user": "remote-client-message",
        }
        assert aliases.segments == {
            ("native-turn", 0): "remote-client-message",
        }

    asyncio.run(run())


def test_replacement_start_followed_by_user_message_becomes_spontaneous_turn():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        logical_turn_id = "managed-before-new-user"
        replacement_turn_id = "native-new-user-turn"
        handle.turn_id = logical_turn_id
        handle.turn_active = True
        handle._open_managed_stream()
        managed_consumer = asyncio.create_task(
            _collect_managed_response(handle))
        await asyncio.sleep(0)

        compacted = _notification("thread/compacted", logical_turn_id)
        old_terminal = _notification(
            "turn/completed", logical_turn_id,
            turn={"id": logical_turn_id, "status": "interrupted"},
        )
        replacement_started = _notification(
            "turn/started", replacement_turn_id,
            turn={"id": replacement_turn_id},
        )
        replacement_user = _notification(
            "item/completed", replacement_turn_id,
            item={
                "id": "replacement-user",
                "type": "userMessage",
                "text": "a genuinely new prompt",
            },
        )
        await handle._dispatch(compacted)
        await handle._dispatch(old_terminal)
        await handle._dispatch(replacement_started)
        await handle._dispatch(replacement_user)

        assert await asyncio.wait_for(managed_consumer, timeout=0.2) == [
            compacted, old_terminal,
        ]
        assert handle.turn_id == replacement_turn_id
        assert handle._spontaneous_turn_id == replacement_turn_id

        replacement_terminal = _notification(
            "turn/completed", replacement_turn_id,
            turn={"id": replacement_turn_id, "status": "completed"},
        )
        await handle._dispatch(replacement_terminal)
        spontaneous = [item async for item in
                       handle.receive_spontaneous_response(replacement_turn_id)]
        assert spontaneous == [
            replacement_started, replacement_user, replacement_terminal,
        ]

    asyncio.run(run())


def test_shared_daemon_accepts_only_exact_unattributed_managed_turn_frames():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        handle.turn_id = "managed-turn"
        handle.turn_active = True
        handle._using_daemon_proxy = True
        handle._open_managed_stream()

        exact = _notification(
            "item/agentMessage/delta", "managed-turn",
            itemId="answer", delta="exact",
        )
        exact["params"].pop("threadId")
        await handle._dispatch(exact)

        foreign = _notification(
            "item/agentMessage/delta", "foreign-turn",
            itemId="foreign", delta="foreign",
        )
        foreign["params"].pop("threadId")
        await handle._dispatch(foreign)

        missing_turn = _notification(
            "item/agentMessage/delta", "managed-turn",
            itemId="missing", delta="missing",
        )
        missing_turn["params"].pop("threadId")
        missing_turn["params"].pop("turnId")
        await handle._dispatch(missing_turn)

        queued = await asyncio.wait_for(handle._turn_q.get(), timeout=0.1)
        assert queued == exact
        assert handle._turn_q.qsize() == 0

    asyncio.run(run())


def test_compaction_replacement_fence_rejects_stale_queue_and_generation():
    async def arm(handle: CodexHandle, turn_id: str) -> None:
        handle.thread_id = "thread-spontaneous"
        handle.turn_id = turn_id
        handle.turn_active = True
        handle._open_managed_stream()
        await handle._dispatch(_notification("thread/compacted", turn_id))
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        ))

    async def run():
        stale_generation = CodexHandle(_Cfg())
        await arm(stale_generation, "generation-old")
        stale_generation._generation += 1
        await stale_generation._dispatch(_notification(
            "turn/started", "generation-new",
            turn={"id": "generation-new"},
        ))
        assert stale_generation.turn_id == "generation-old"

        stale_queue = CodexHandle(_Cfg())
        await arm(stale_queue, "queue-old")
        stale_queue._open_managed_stream()
        await stale_queue._dispatch(_notification(
            "turn/started", "queue-new", turn={"id": "queue-new"},
        ))
        assert stale_queue.turn_id == "queue-old"

    asyncio.run(run())


def test_compaction_interrupted_boundary_expires_to_a_real_terminal(monkeypatch):
    async def run():
        monkeypatch.setattr(
            codex_handle_module,
            "_COMPACTION_CONTINUATION_GRACE_SECONDS",
            0.01,
        )
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = "managed-compact-timeout"
        handle.turn_id = turn_id
        handle.turn_active = True
        handle._open_managed_stream()

        compacted = _notification("thread/compacted", turn_id)
        terminal = _notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        )
        await handle._dispatch(compacted)
        await handle._dispatch(terminal)

        frames = await asyncio.wait_for(
            _collect_managed_response(handle), timeout=0.2)
        assert frames == [compacted, terminal]
        assert handle.compaction_continuation_turn_ids == frozenset()
        assert handle.turn_active is False
        assert handle.turn_id is None

    asyncio.run(run())


def test_compaction_deadline_probe_keeps_exact_active_turn_open(monkeypatch):
    async def run():
        monkeypatch.setattr(
            codex_handle_module,
            "_COMPACTION_CONTINUATION_GRACE_SECONDS",
            0.01,
        )
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = "managed-compact-active-probe"
        handle.turn_id = turn_id
        handle.turn_active = True
        handle._open_managed_stream()
        requests = []

        async def request(method, params=None):
            requests.append((method, params))
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thread-spontaneous",
                        "status": {"type": "active", "activeFlags": []},
                    },
                }
            assert method == "thread/turns/list"
            return {
                "data": [{"id": turn_id, "status": "inProgress"}],
                "nextCursor": None,
            }

        handle._request = request
        compacted = _context_compaction(turn_id)
        await handle._dispatch(compacted)
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        ))
        fence = handle._managed_compaction_continuation
        assert fence is not None
        await asyncio.wait_for(fence.settled.wait(), timeout=0.2)
        assert fence.awaiting_replacement is False
        assert fence.suppressed_terminal is None
        assert handle.turn_active is True
        assert requests == [
            ("thread/read", {
                "threadId": "thread-spontaneous",
                "includeTurns": False,
            }),
            ("thread/turns/list", {
                "threadId": "thread-spontaneous",
                "cursor": None,
                "limit": 1,
                "sortDirection": "desc",
                "itemsView": "notLoaded",
            }),
        ]

        continuation = _notification(
            "item/completed", turn_id,
            item={
                "id": "after-active-probe",
                "type": "agentMessage",
                "text": "continued after the five second boundary",
            },
        )
        final = _notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "completed"},
        )
        await handle._dispatch(continuation)
        await handle._dispatch(final)
        assert [item async for item in handle.receive_response()] == [
            compacted, continuation, final,
        ]

    asyncio.run(run())


@pytest.mark.parametrize("probe_mode", [
    "idle", "different-turn", "timeout", "error",
])
def test_compaction_deadline_probe_fails_closed(monkeypatch, probe_mode):
    async def run():
        monkeypatch.setattr(
            codex_handle_module,
            "_COMPACTION_CONTINUATION_GRACE_SECONDS",
            0.01,
        )
        monkeypatch.setattr(
            codex_handle_module,
            "_COMPACTION_CONTINUATION_PROBE_TIMEOUT_SECONDS",
            0.02,
        )
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = f"managed-compact-{probe_mode}"
        handle.turn_id = turn_id
        handle.turn_active = True
        handle._open_managed_stream()

        async def request(method, params=None):
            if probe_mode == "timeout":
                await asyncio.Event().wait()
            if probe_mode == "error":
                raise RuntimeError("probe failed")
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thread-spontaneous",
                        "status": {"type": (
                            "idle" if probe_mode == "idle" else "active"
                        )},
                    },
                }
            return {
                "data": [{
                    "id": "another-turn",
                    "status": "inProgress",
                }],
            }

        handle._request = request
        compacted = _context_compaction(turn_id)
        terminal = _notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        )
        await handle._dispatch(compacted)
        await handle._dispatch(terminal)
        assert await asyncio.wait_for(
            _collect_managed_response(handle), timeout=0.3,
        ) == [compacted, terminal]
        assert handle.turn_active is False
        assert handle.turn_id is None

    asyncio.run(run())


def test_late_continuation_wins_while_compaction_probe_is_in_flight(monkeypatch):
    async def run():
        monkeypatch.setattr(
            codex_handle_module,
            "_COMPACTION_CONTINUATION_GRACE_SECONDS",
            0.01,
        )
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = "managed-compact-late-during-probe"
        handle.turn_id = turn_id
        handle.turn_active = True
        handle._open_managed_stream()
        probe_started = asyncio.Event()
        release_probe = asyncio.Event()

        async def request(method, params=None):
            probe_started.set()
            await release_probe.wait()
            return {
                "thread": {
                    "id": "thread-spontaneous",
                    "status": {"type": "idle"},
                },
            }

        handle._request = request
        compacted = _context_compaction(turn_id)
        await handle._dispatch(compacted)
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        ))
        await asyncio.wait_for(probe_started.wait(), timeout=0.2)
        fence = handle._managed_compaction_continuation
        assert fence is not None and fence.probing is True

        continuation = _notification(
            "item/completed", turn_id,
            item={
                "id": "late-continuation-during-probe",
                "type": "agentMessage",
                "text": "late continuation",
            },
        )
        await handle._dispatch(continuation)
        assert fence.awaiting_replacement is False
        release_probe.set()
        await asyncio.sleep(0)
        final = _notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "completed"},
        )
        await handle._dispatch(final)
        assert [item async for item in handle.receive_response()] == [
            compacted, continuation, final,
        ]

    asyncio.run(run())


def test_continuation_wins_after_final_compaction_probe_reply(monkeypatch):
    async def run():
        monkeypatch.setattr(
            codex_handle_module,
            "_COMPACTION_CONTINUATION_GRACE_SECONDS",
            0.01,
        )
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = "managed-compact-final-probe-race"
        handle.turn_id = turn_id
        handle.turn_active = True
        handle._open_managed_stream()
        continuation = _notification(
            "item/completed", turn_id,
            item={
                "id": "continuation-at-final-probe-reply",
                "type": "agentMessage",
                "text": "continued",
            },
        )

        async def request(method, params=None):
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thread-spontaneous",
                        "status": {"type": "active"},
                    },
                }
            # Model the reader delivering a continuation immediately before it
            # resolves the final RPC response to the detached probe task.
            await handle._dispatch(continuation)
            return {"data": [{"id": turn_id, "status": "completed"}]}

        handle._request = request
        compacted = _context_compaction(turn_id)
        await handle._dispatch(compacted)
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        ))
        fence = handle._managed_compaction_continuation
        assert fence is not None
        await asyncio.wait_for(fence.settled.wait(), timeout=0.2)
        await asyncio.sleep(0)

        final = _notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "completed"},
        )
        await handle._dispatch(final)
        assert [item async for item in handle.receive_response()] == [
            compacted, continuation, final,
        ]

    asyncio.run(run())


def test_compaction_probe_cannot_cross_generation_or_queue(monkeypatch):
    async def run(stale_kind):
        monkeypatch.setattr(
            codex_handle_module,
            "_COMPACTION_CONTINUATION_GRACE_SECONDS",
            0.01,
        )
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = f"managed-compact-stale-{stale_kind}"
        handle.turn_id = turn_id
        handle.turn_active = True
        handle._open_managed_stream()
        old_queue = handle._turn_q
        probe_started = asyncio.Event()
        release_probe = asyncio.Event()

        async def request(method, params=None):
            probe_started.set()
            await release_probe.wait()
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thread-spontaneous",
                        "status": {"type": "active"},
                    },
                }
            return {"data": [{"id": turn_id, "status": "inProgress"}]}

        handle._request = request
        await handle._dispatch(_context_compaction(turn_id))
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        ))
        await asyncio.wait_for(probe_started.wait(), timeout=0.2)
        if stale_kind == "generation":
            handle._generation += 1
        else:
            handle._open_managed_stream()
        release_probe.set()
        await asyncio.sleep(0.02)
        assert handle._managed_compaction_continuation is None
        # No stale terminal may be injected into a replacement queue.
        assert handle._turn_q is not old_queue or stale_kind == "generation"
        if handle._turn_q is not old_queue:
            assert handle._turn_q.qsize() == 0

    asyncio.run(run("generation"))
    asyncio.run(run("queue"))


def test_unconfirmed_replacement_start_is_replayed_after_compaction_timeout(
    monkeypatch,
):
    async def run():
        monkeypatch.setattr(
            codex_handle_module,
            "_COMPACTION_CONTINUATION_GRACE_SECONDS",
            0.01,
        )
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        logical_turn_id = "managed-before-replacement-timeout"
        replacement_turn_id = "replacement-after-timeout"
        handle.turn_id = logical_turn_id
        handle.turn_active = True
        handle._open_managed_stream()
        managed_consumer = asyncio.create_task(
            _collect_managed_response(handle))
        await asyncio.sleep(0)

        compacted = _notification("thread/compacted", logical_turn_id)
        old_terminal = _notification(
            "turn/completed", logical_turn_id,
            turn={"id": logical_turn_id, "status": "interrupted"},
        )
        replacement_started = _notification(
            "turn/started", replacement_turn_id,
            turn={"id": replacement_turn_id},
        )
        await handle._dispatch(compacted)
        await handle._dispatch(old_terminal)
        await handle._dispatch(replacement_started)

        assert await asyncio.wait_for(managed_consumer, timeout=0.2) == [
            compacted, old_terminal,
        ]
        assert handle.turn_id == replacement_turn_id
        assert handle._spontaneous_turn_id == replacement_turn_id

        replacement_terminal = _notification(
            "turn/completed", replacement_turn_id,
            turn={"id": replacement_turn_id, "status": "completed"},
        )
        await handle._dispatch(replacement_terminal)
        assert [item async for item in
                handle.receive_spontaneous_response(replacement_turn_id)] == [
            replacement_started, replacement_terminal,
        ]

    asyncio.run(run())


def test_steer_waits_for_compaction_replacement_attribution():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        logical_turn_id = "managed-before-grace-steer"
        replacement_turn_id = "replacement-for-grace-steer"
        handle.turn_id = logical_turn_id
        handle.turn_active = True
        handle._open_managed_stream()
        handle.proc = SimpleNamespace(returncode=None)

        requests = []

        async def request(method, params, **kwargs):
            requests.append((method, params, kwargs))
            return {"turnId": params.get("expectedTurnId")}

        handle._request = request
        await handle._dispatch(_notification(
            "thread/compacted", logical_turn_id,
        ))
        await handle._dispatch(_notification(
            "turn/completed", logical_turn_id,
            turn={"id": logical_turn_id, "status": "interrupted"},
        ))

        steering = asyncio.create_task(handle.steer("guide it"))
        await asyncio.sleep(0)
        assert not steering.done()
        assert requests == []

        await handle._dispatch(_notification(
            "turn/started", replacement_turn_id,
            turn={"id": replacement_turn_id},
        ))
        assert handle.compaction_continuation_turn_ids == frozenset({
            logical_turn_id,
        })
        await asyncio.sleep(0)
        assert not steering.done()
        await handle._dispatch(_notification(
            "item/agentMessage/delta", replacement_turn_id,
            itemId="replacement-answer", delta="continuing",
        ))

        acceptance = await asyncio.wait_for(steering, timeout=0.2)
        assert str(acceptance) == replacement_turn_id
        assert requests[0][0:2] == (
            "turn/steer",
            {
                "threadId": "thread-spontaneous",
                "expectedTurnId": replacement_turn_id,
                "input": [{"type": "text", "text": "guide it"}],
            },
        )
        handle._discard_managed_compaction_continuation()

    asyncio.run(run())


def test_disconnect_cancels_compaction_expiry_and_wakes_managed_consumer():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = "managed-compact-disconnect"
        handle.turn_id = turn_id
        handle.turn_active = True
        handle._open_managed_stream()
        consumer = asyncio.create_task(_collect_managed_response(handle))
        await asyncio.sleep(0)

        compacted = _notification("thread/compacted", turn_id)
        await handle._dispatch(compacted)
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        ))
        fence = handle._managed_compaction_continuation
        assert fence is not None
        assert fence.expiry_task is not None

        await handle.disconnect()
        frames = await asyncio.wait_for(consumer, timeout=0.2)
        assert frames == [compacted]
        assert handle._managed_compaction_continuation is None
        assert fence.expiry_task is None
        assert handle.turn_active is False
        assert handle.turn_id is None

    asyncio.run(run())


def test_unexpected_eof_cancels_compaction_expiry_and_wakes_managed_consumer():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = "managed-compact-eof"
        handle.turn_id = turn_id
        handle.turn_active = True
        handle._open_managed_stream()
        consumer = asyncio.create_task(_collect_managed_response(handle))
        await asyncio.sleep(0)

        compacted = _notification("thread/compacted", turn_id)
        await handle._dispatch(compacted)
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        ))
        fence = handle._managed_compaction_continuation
        assert fence is not None
        assert fence.expiry_task is not None

        class EofStdout:
            async def readline(self):
                return b""

        await handle._read_loop(
            SimpleNamespace(stdout=EofStdout()), handle._generation)
        frames = await asyncio.wait_for(consumer, timeout=0.2)
        assert frames == [compacted]
        assert handle._managed_compaction_continuation is None
        assert fence.expiry_task is None
        assert handle.turn_active is False

    asyncio.run(run())


def test_user_interrupt_targets_replacement_native_id_but_closes_logical_turn():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        logical_turn_id = "managed-before-user-interrupt"
        replacement_turn_id = "managed-native-at-interrupt"
        handle.turn_id = logical_turn_id
        handle.turn_active = True
        handle._open_managed_stream()

        await handle._dispatch(_notification(
            "thread/compacted", logical_turn_id,
        ))
        await handle._dispatch(_notification(
            "turn/completed", logical_turn_id,
            turn={"id": logical_turn_id, "status": "interrupted"},
        ))
        await handle._dispatch(_notification(
            "turn/started", replacement_turn_id,
            turn={"id": replacement_turn_id},
        ))

        requests = []

        async def request(method, params):
            requests.append((method, params))
            return {}

        handle.proc = SimpleNamespace(returncode=None)
        handle._request = request
        await handle.interrupt()
        assert handle.compaction_continuation_turn_ids == frozenset()
        assert requests == [("turn/interrupt", {
            "threadId": "thread-spontaneous",
            "turnId": replacement_turn_id,
        })]

        await handle._dispatch(_notification(
            "turn/completed", replacement_turn_id,
            turn={"id": replacement_turn_id, "status": "interrupted"},
        ))
        frames = [item async for item in handle.receive_response()]
        assert frames[-1]["params"]["turnId"] == logical_turn_id
        assert frames[-1]["params"]["turn"]["id"] == logical_turn_id
        assert frames[-1]["params"]["turn"]["status"] == "interrupted"
        assert handle.turn_active is False

    asyncio.run(run())


async def _collect_managed_response(handle: CodexHandle):
    return [item async for item in handle.receive_response()]


def test_plain_interrupted_boundary_remains_terminal():
    async def run():
        lifecycle = []

        async def on_lifecycle(phase, turn_id):
            lifecycle.append((phase, turn_id))

        handle = CodexHandle(_Cfg(), turn_lifecycle_callback=on_lifecycle)
        handle.thread_id = "thread-spontaneous"
        turn_id = "plain-interrupted"
        await handle._dispatch(_notification(
            "turn/started", turn_id, turn={"id": turn_id},
        ))
        terminal = _notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        )
        await handle._dispatch(terminal)

        frames = [
            item async for item in
            handle.receive_spontaneous_response(turn_id)
        ]
        assert frames[-1] == terminal
        assert lifecycle == [
            ("started", turn_id),
            ("completed", turn_id),
        ]
        assert handle.turn_active is False
        assert handle.turn_id is None

    asyncio.run(run())


def test_stdout_reader_drains_burst_when_spontaneous_consumer_is_stalled():
    async def run():
        turn_id = "auto-burst"
        messages = [
            _notification("turn/started", turn_id, turn={"id": turn_id}),
            *[
                _notification("item/agentMessage/delta", turn_id,
                              itemId="answer", delta=str(index))
                for index in range(96)
            ],
            _notification("turn/completed", turn_id,
                          turn={"id": turn_id, "status": "completed"}),
        ]
        lines = [json.dumps(message).encode() + b"\n" for message in messages]
        lines.append(b"")

        class Stdout:
            reads = 0

            async def readline(self):
                self.reads += 1
                return lines.pop(0)

        stdout = Stdout()
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        await asyncio.wait_for(handle._read_loop(
            SimpleNamespace(stdout=stdout), handle._generation), timeout=0.5)
        assert stdout.reads == len(messages) + 1

        items = [item async for item in
                 handle.receive_spontaneous_response(turn_id)]
        assert isinstance(items[0], CodexSpontaneousOverflow)
        assert items[-1]["method"] == "turn/completed"

    asyncio.run(run())


def test_spontaneous_bridge_closes_on_disconnect_without_raw_error_data():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        await handle._dispatch(_notification(
            "turn/started", "auto-closed", turn={"id": "auto-closed"},
        ))
        await handle.disconnect()
        items = [item async for item in
                 handle.receive_spontaneous_response("auto-closed")]
        assert items[0]["method"] == "turn/started"
        assert isinstance(items[-1], CodexSpontaneousClosed)

    asyncio.run(run())


def test_managed_turn_never_double_routes_into_spontaneous_bridge():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        handle.turn_active = True
        handle._turn_q = asyncio.Queue()
        await handle._dispatch(_notification(
            "turn/started", "managed-turn", turn={"id": "managed-turn"},
        ))
        delta = _notification(
            "item/agentMessage/delta", "managed-turn",
            itemId="answer", delta="managed",
        )
        await handle._dispatch(delta)
        assert handle._spontaneous_q is None
        assert handle._spontaneous_turn_id is None
        assert (await handle._turn_q.get())["method"] == "turn/started"
        assert await handle._turn_q.get() == delta

    asyncio.run(run())


def test_old_managed_consumer_cannot_clear_new_spontaneous_active_flag():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        old_queue = asyncio.Queue()
        handle._turn_q = old_queue
        handle.turn_active = False

        async def consume_old_queue():
            return [message async for message in handle.receive_response()]

        consumer = asyncio.create_task(consume_old_queue())
        await asyncio.sleep(0)
        await handle._dispatch(_notification(
            "turn/started", "auto-after-managed",
            turn={"id": "auto-after-managed"},
        ))
        old_queue.put_nowait(None)
        assert await consumer == []
        assert handle.turn_active is True
        assert handle._spontaneous_turn_id == "auto-after-managed"

    asyncio.run(run())


@pytest.mark.parametrize("hold_at", ["terminal", "cleanup", "before_consumer"])
@pytest.mark.parametrize("successor_outcome", ["completed", "interrupted", "closed"])
def test_goal_successor_waits_for_old_projection_without_becoming_idle(
    monkeypatch, hold_at, successor_outcome,
):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        ctx.codex_checkpoint = False
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.turn_lifecycle_callback = (
            lambda phase, turn_id: machine._on_codex_turn_lifecycle(
                ctx, phase, turn_id))
        held, release = asyncio.Event(), asyncio.Event()
        original_send = transport.send
        original_cleanup = machine._cleanup_codex_steer_attachments

        async def send(event):
            if (hold_at == "terminal" and isinstance(event, TurnEnd)
                    and event.turn_id == "old-goal"):
                held.set()
                await release.wait()
            await original_send(event)

        async def cleanup(context):
            if hold_at == "cleanup" and not held.is_set():
                held.set()
                await release.wait()
            await original_cleanup(context)

        monkeypatch.setattr(transport, "send", send)
        monkeypatch.setattr(machine, "_cleanup_codex_steer_attachments", cleanup)
        monkeypatch.setattr(machine, "_schedule_history_refresh", lambda *a, **kw: None)

        async def frame(method, turn_id, **params):
            await handle._dispatch(_notification(method, turn_id, **params))

        await frame("turn/started", "old-goal", turn={"id": "old-goal"})
        await frame("item/completed", "old-goal", item={
            "id": "old-answer", "type": "agentMessage",
            "text": "old answer", "phase": "final_answer",
        })
        await frame("turn/completed", "old-goal", turn={
            "id": "old-goal", "status": "completed",
        })
        old_task = ctx.codex_spontaneous_task
        if hold_at != "before_consumer":
            await asyncio.wait_for(held.wait(), 2)
        await frame("turn/started", "next-goal", turn={"id": "next-goal"})
        await frame("item/completed", "next-goal", item={
            "id": "next-answer", "type": "agentMessage",
            "text": "next answer", "phase": "final_answer",
        })
        if successor_outcome == "interrupted":
            calls = []

            async def request(method, params):
                calls.append((method, params))
                return {}

            monkeypatch.setattr(handle, "_request", request)
            ctx.state = "interrupting"
            ctx.interrupt_event.set()
            await handle.interrupt()
            assert calls == [("turn/interrupt", {
                "threadId": "thread-spontaneous", "turnId": "next-goal",
            })]
        release.set()
        await asyncio.wait_for(old_task, 2)
        # Wait for actual output, not a scheduling delay, before inspecting the
        # session which History uses to classify its unfinished newest row.
        async def successor_visible():
            while not any(isinstance(e, Delta) and e.text == "next answer"
                          for e in transport.sent):
                await asyncio.sleep(0)
        try:
            await asyncio.wait_for(successor_visible(), 2)
            assert ctx.state == (
                "interrupting" if successor_outcome == "interrupted" else "running")
            if successor_outcome == "interrupted":
                assert ctx.interrupt_event.is_set()
            assert ctx.codex_spontaneous_turn_id == "next-goal"
            assert handle.turn_active
            assert not any(isinstance(e, StateEvent) and e.state == "idle"
                           for e in transport.sent)
            old_end = next(i for i, e in enumerate(transport.sent)
                           if isinstance(e, TurnEnd) and e.turn_id == "old-goal")
            new_start = next(i for i, e in enumerate(transport.sent)
                             if isinstance(e, UserMsg) and e.msg_id == "next-goal")
            assert old_end < new_start
            new_binding = next(i for i, e in enumerate(transport.sent)
                               if isinstance(e, TurnBinding)
                               and e.msg_id == e.turn_id == "next-goal")
            new_state = next(i for i, e in enumerate(transport.sent)
                             if isinstance(e, StateEvent)
                             and e.msg_id == "next-goal")
            assert old_end < new_binding < new_state < new_start
            assert transport.sent[new_state].state == ctx.state
            # Reconnect must receive the same exact owner, even though the
            # session never went idle between the two automatic turns.
            assert ctx.active_turn_binding is not None
            assert ctx.active_turn_binding.msg_id == "next-goal"
            assert ctx.active_turn_binding.turn_id == "next-goal"
            task = ctx.codex_spontaneous_task
            if successor_outcome == "closed":
                handle._close_spontaneous_stream("next-goal")
            else:
                await frame("turn/completed", "next-goal", turn={
                    "id": "next-goal", "status": successor_outcome,
                })
            await asyncio.wait_for(task, 2)
            assert ctx.state == "idle"
            assert [e.turn_id for e in transport.sent if isinstance(e, TurnEnd)] == [
                "old-goal", "next-goal",
            ]
            ends = [e for e in transport.sent if isinstance(e, TurnEnd)]
            assert ends[0].result.subtype == "success"
            assert ends[-1].result.subtype == {
                "completed": "success", "interrupted": "error_during_execution",
                "closed": "error",
            }[successor_outcome]
            if successor_outcome == "completed":
                assert not any(isinstance(e, Error) for e in transport.sent)
        finally:
            release.set()
            task = ctx.codex_spontaneous_task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("cancel_consumer", [False, True])
def test_goal_successor_burst_is_bounded_and_recovers_history(monkeypatch, cancel_consumer):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        ctx.codex_checkpoint = False
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.turn_lifecycle_callback = (
            lambda phase, turn_id: machine._on_codex_turn_lifecycle(
                ctx, phase, turn_id))
        held, release = asyncio.Event(), asyncio.Event()
        original_send = transport.send
        original_retire = machine._retire_codex_checkpoint
        refreshed, retired = [], []

        async def send(event):
            if isinstance(event, TurnEnd) and event.turn_id == "goal-0":
                held.set()
                await release.wait()
            await original_send(event)

        async def retire(context, **kwargs):
            retired.append(kwargs)
            await original_retire(context, **kwargs)

        monkeypatch.setattr(transport, "send", send)
        monkeypatch.setattr(machine, "_retire_codex_checkpoint", retire)
        monkeypatch.setattr(machine, "_schedule_history_refresh",
                            lambda sid, **kw: refreshed.append(sid))

        async def finished_turn(turn_id):
            for message in [
                _notification("turn/started", turn_id, turn={"id": turn_id}),
                _notification("item/completed", turn_id, item={
                    "id": "answer-" + turn_id, "type": "agentMessage",
                    "text": turn_id, "phase": "final_answer",
                }),
                _notification("turn/completed", turn_id, turn={
                    "id": turn_id, "status": "completed",
                }),
            ]:
                await handle._dispatch(message)

        await finished_turn("goal-0")
        task = ctx.codex_spontaneous_task
        await asyncio.wait_for(held.wait(), 2)
        try:
            for i in range(1, 20):
                await finished_turn(f"goal-{i}")
                assert ctx.codex_spontaneous_task is task
                assert ctx.codex_spontaneous_next_id == f"goal-{i}"
                assert ctx.state == "running"
            error = await machine._handle_steer(Steer(
                sid=ctx.key, msg_id="steer-during-handoff", prompt="continue",
                cmd_id="steer-command", client_id="client",
            ))
            assert isinstance(error, Error)
            assert "切换自动回合" in error.message
            if cancel_consumer:
                task.cancel()
            else:
                release.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
            successor = ctx.codex_spontaneous_task
            if cancel_consumer:
                assert successor is None
            elif successor is not None:
                await asyncio.wait_for(successor, 2)
            assert ctx.state == "idle"
            assert ctx.codex_spontaneous_next_id is None
            assert refreshed and retired
            ends = [e for e in transport.sent if isinstance(e, TurnEnd)]
            if not cancel_consumer:
                assert [e.turn_id for e in ends] == ["goal-0", "goal-19"]
                assert all(e.result.subtype == "success" for e in ends)
                assert any(isinstance(e, Delta) and e.text == "goal-19"
                           for e in transport.sent)
        finally:
            release.set()
            pending = ctx.codex_spontaneous_task
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("race_at", ["relay", "anchor", "waiting", "interrupt"])
def test_account_switch_belongs_to_queued_goal_successor(monkeypatch, race_at):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine, ctx.space = "codex", "code"
        ctx.codex_checkpoint = False
        ctx.codex_daemon_epoch = "old-epoch"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.turn_lifecycle_callback = (
            lambda phase, turn_id: machine._on_codex_turn_lifecycle(ctx, phase, turn_id))
        held, release, marker, resumed = (asyncio.Event() for _ in range(4))
        switch = CodexDaemonRestartState("new-epoch", "ready", 1, 10**12)
        interrupts, reconnects, goal_resumes = [], [], []
        original_send = transport.send

        async def frame(method, turn_id, **params):
            await handle._dispatch(_notification(method, turn_id, **params))

        async def answer(turn_id):
            await frame("item/completed", turn_id, item={
                "id": "answer-" + turn_id, "type": "agentMessage",
                "text": turn_id, "phase": "final_answer",
            })

        async def send(event):
            if (ctx.codex_spontaneous_turn_id == "old-goal" and (
                race_at == "relay" and isinstance(event, Delta)
                or race_at == "waiting" and isinstance(event, StateEvent)
                and event.phase == "waiting"
            )):
                held.set()
                await release.wait()
            await original_send(event)

        async def recover_user(_sid, turn_id, *_args, **_kwargs):
            if race_at == "anchor" and turn_id == "old-goal":
                held.set()
                await release.wait()
            return None

        async def watch(_ctx, *, starting_epoch):
            if starting_epoch == switch.epoch:
                await asyncio.Event().wait()
            await marker.wait()
            return switch

        async def restart_state(_ctx, **_kwargs):
            return switch

        async def request(method, params):
            assert method == "turn/interrupt"
            target = params["turnId"]
            interrupts.append((ctx.codex_spontaneous_turn_id, target))
            if race_at == "interrupt" and target == "old-goal":
                held.set()
                await release.wait()
            else:
                await frame("turn/completed", target, turn={
                    "id": target, "status": "interrupted",
                })
            return {}

        async def reconnect(_ctx, **_kwargs):
            reconnects.append(ctx.codex_spontaneous_anchor_id)
            ctx.codex_daemon_epoch = switch.epoch
            return True

        async def goal():
            return {"status": "active"}

        async def resume_goal(_ctx, _goal):
            goal_resumes.append(ctx.codex_spontaneous_anchor_id)
            await frame("turn/started", "replacement-goal", turn={"id": "replacement-goal"})
            resumed.set()
            return True

        monkeypatch.setattr(transport, "send", send)
        monkeypatch.setattr(machine, "_recover_official_codex_user", recover_user)
        monkeypatch.setattr(machine, "_codex_shared_affinity", lambda _ctx: True)
        monkeypatch.setattr(machine, "_wait_for_codex_account_switch", watch)
        monkeypatch.setattr(machine, "_codex_restart_state_for_ctx", restart_state)
        monkeypatch.setattr(machine, "_read_codex_restart_state_for_ctx", lambda _ctx: switch)
        monkeypatch.setattr(machine, "_ensure_codex_daemon_generation", reconnect)
        monkeypatch.setattr(machine, "_resume_codex_goal_after_account_switch", resume_goal)
        monkeypatch.setattr(machine, "_schedule_history_refresh", lambda *a, **kw: None)
        monkeypatch.setattr(handle, "_request", request)
        monkeypatch.setattr(handle, "get_goal", goal)

        await frame("turn/started", "old-goal", turn={"id": "old-goal"})
        old_task = ctx.codex_spontaneous_task
        try:
            if race_at == "relay":
                await answer("old-goal")
            else:
                marker.set()
            await asyncio.wait_for(held.wait(), 2)
            if race_at != "relay":
                await answer("old-goal")
            await frame("turn/completed", "old-goal", turn={"id": "old-goal", "status": "completed"})
            await frame("turn/started", "next-goal", turn={"id": "next-goal"})
            await answer("next-goal")
            assert ctx.codex_spontaneous_next_id == "next-goal"
            marker.set()
            release.set()
            await asyncio.wait_for(resumed.wait(), 2)
            await asyncio.wait_for(old_task, 2)
            expected = [("old-goal", "old-goal")] if race_at == "interrupt" else []
            assert interrupts == [*expected, ("next-goal", "next-goal")]
            assert reconnects == ["next-goal"]
            assert goal_resumes == ["next-goal"]
            ends = [e for e in transport.sent if isinstance(e, TurnEnd)]
            assert [(e.turn_id, e.result.subtype) for e in ends] == [("old-goal", "success")]
            assert ctx.state == "running"
            assert not any(isinstance(e, StateEvent) and e.state == "idle" for e in transport.sent)
            replacement = ctx.codex_spontaneous_task
            await answer("replacement-goal")
            await frame("turn/completed", "replacement-goal", turn={
                "id": "replacement-goal", "status": "completed",
            })
            await asyncio.wait_for(replacement, 2)
            assert ctx.state == "idle"
            assert not any(isinstance(e, Error) for e in transport.sent)
        finally:
            release.set()
            pending = {old_task, ctx.codex_spontaneous_task} - {None}
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(run())


def test_spontaneous_successor_requires_exact_native_terminal():
    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        ctx.state = "running"
        ctx.codex_spontaneous_turn_id = "still-running"
        ctx.codex_spontaneous_task = asyncio.create_task(asyncio.Event().wait())
        try:
            await machine._on_codex_turn_lifecycle(ctx, "completed", "foreign-turn")
            await machine._on_codex_turn_lifecycle(ctx, "started", "overlap")
            assert ctx.codex_spontaneous_completed_id is None
            assert ctx.codex_spontaneous_next_id is None
            assert ctx.codex_spontaneous_turn_id == "still-running"
        finally:
            ctx.codex_spontaneous_task.cancel()
            await asyncio.gather(ctx.codex_spontaneous_task, return_exceptions=True)

    asyncio.run(run())


def test_machine_streams_rich_spontaneous_turn_and_unlocks_on_matching_terminal():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.turn_lifecycle_callback = (
            lambda phase, turn_id: machine._on_codex_turn_lifecycle(
                ctx, phase, turn_id))

        turn_id = "auto-rich"
        messages = [
            _notification("turn/started", turn_id, turn={"id": turn_id}),
            _notification("item/reasoning/summaryPartAdded", turn_id,
                          itemId="reasoning-1", summaryIndex=0),
            _notification("item/reasoning/summaryTextDelta", turn_id,
                          itemId="reasoning-1", summaryIndex=0,
                          delta="公开思考摘要"),
            _notification("turn/plan/updated", turn_id,
                          explanation="执行计划",
                          plan=[{"step": "检查", "status": "inProgress"}]),
            _notification("item/started", turn_id, item={
                "type": "commandExecution", "id": "command-1",
                "command": "pwd", "cwd": "/repo", "status": "inProgress",
                "commandActions": [],
            }),
            _notification("item/commandExecution/outputDelta", turn_id,
                          itemId="command-1", delta="/repo\n"),
            _notification("item/completed", turn_id, item={
                "type": "commandExecution", "id": "command-1",
                "command": "pwd", "cwd": "/repo", "status": "completed",
                "commandActions": [], "aggregatedOutput": "/repo\n",
                "exitCode": 0, "durationMs": 4,
            }),
            _notification("turn/diff/updated", turn_id,
                          diff="@@ -1 +1 @@\n-old\n+new"),
            _notification("item/started", turn_id, item={
                "type": "mcpToolCall", "id": "mcp-1", "server": "docs",
                "tool": "lookup", "status": "inProgress",
                "arguments": {"query": "sdk"},
            }),
            _notification("item/mcpToolCall/progress", turn_id,
                          itemId="mcp-1", message="50%"),
            _notification("item/completed", turn_id, item={
                "type": "mcpToolCall", "id": "mcp-1", "server": "docs",
                "tool": "lookup", "status": "completed",
                "arguments": {"query": "sdk"},
                "result": {"content": [{"type": "text", "text": "ok"}]},
                "durationMs": 5,
            }),
            _notification("item/started", turn_id, item={
                "type": "collabAgentToolCall", "id": "agent-1",
                "tool": "spawnAgent", "status": "inProgress",
                "senderThreadId": "thread-spontaneous",
                "receiverThreadIds": ["child-1"], "prompt": "inspect",
            }),
            _notification("hook/completed", turn_id, run={
                "id": "hook-1", "eventName": "preToolUse",
                "handlerType": "command", "status": "completed",
                "durationMs": 2,
            }),
            _notification("item/completed", turn_id, item={
                "type": "agentMessage", "id": "answer-1",
                "text": "最终答案", "phase": "final_answer",
            }),
            _notification("turn/completed", turn_id, turn={
                "id": turn_id, "status": "completed", "durationMs": 25,
            }),
        ]
        for message in messages:
            await handle._dispatch(message)

        task = ctx.codex_spontaneous_task
        assert task is not None
        await asyncio.wait_for(task, timeout=1)

        assert ctx.state == "idle"
        assert ctx.codex_spontaneous_turn_id is None
        anchors = [event for event in transport.sent if isinstance(event, UserMsg)]
        assert [(event.msg_id, event.prompt) for event in anchors] == [(turn_id, "")]
        assert [event.state for event in transport.sent
                if isinstance(event, StateEvent)] == ["running", "idle"]
        assert any(isinstance(event, Delta) and event.text == "最终答案"
                   for event in transport.sent)
        assert any(isinstance(event, TurnPlan) for event in transport.sent)
        assert any(isinstance(event, TurnDiff) for event in transport.sent)
        assert {event.category for event in transport.sent
                if isinstance(event, ToolUse)} == {"command", "mcp"}
        assert any(isinstance(event, ToolDelta) for event in transport.sent)
        assert len([event for event in transport.sent
                    if isinstance(event, ToolResult)]) == 2
        assert {event.kind for event in transport.sent
                if isinstance(event, ProcessEvent)} >= {"reasoning", "agent", "hook"}
        terminal = [event for event in transport.sent if isinstance(event, TurnEnd)]
        assert len(terminal) == 1
        assert terminal[0].turn_id == turn_id
        assert terminal[0].result.subtype == "success"

    asyncio.run(run())


def test_machine_uses_live_cli_user_item_as_spontaneous_turn_identity():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.turn_lifecycle_callback = (
            lambda phase, turn_id: machine._on_codex_turn_lifecycle(
                ctx, phase, turn_id))

        turn_id = "cli-native-turn"
        user = {
            "id": "cli-user-item",
            "type": "userMessage",
            "content": [{"type": "text", "text": "sent from the CLI"}],
        }
        for message in [
            _notification("turn/started", turn_id, turn={"id": turn_id}),
            _notification("item/started", turn_id, item=user),
            _notification("item/completed", turn_id, item=user),
            _notification("item/completed", turn_id, item={
                "id": "cli-answer", "type": "agentMessage",
                "text": "seen remotely", "phase": "final_answer",
            }),
            _notification("turn/completed", turn_id, turn={
                "id": turn_id, "status": "completed",
            }),
        ]:
            await handle._dispatch(message)

        task = ctx.codex_spontaneous_task
        assert task is not None
        await asyncio.wait_for(task, timeout=1)

        users = [event for event in transport.sent
                 if isinstance(event, UserMsg)]
        assert [(event.msg_id, event.prompt) for event in users] == [
            ("cli-user-item", "sent from the CLI"),
        ]
        bindings = [event for event in transport.sent
                    if isinstance(event, TurnBinding)]
        assert [(event.msg_id, event.turn_id) for event in bindings] == [
            (turn_id, turn_id),
            ("cli-user-item", turn_id),
        ]
        assert not any(event.prompt == "" for event in users)
        assert any(isinstance(event, Delta) and event.text == "seen remotely"
                   for event in transport.sent)

    asyncio.run(run())


def test_machine_recovers_missed_cli_user_before_first_output(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.turn_lifecycle_callback = (
            lambda phase, turn_id: machine._on_codex_turn_lifecycle(
                ctx, phase, turn_id))
        recoveries = []
        refreshes = []

        async def recover(sid, native_turn_id, visible_turn_id, user_index,
                          **kwargs):
            recoveries.append((
                sid,
                native_turn_id,
                visible_turn_id,
                user_index,
                kwargs,
            ))
            return UserMsg(
                msg_id="persisted-cli-user",
                prompt="persisted CLI prompt",
            )

        monkeypatch.setattr(
            machine, "_recover_official_codex_user", recover)
        monkeypatch.setattr(
            machine,
            "_schedule_history_refresh",
            lambda sid, **kwargs: refreshes.append((sid, kwargs)),
        )

        turn_id = "cli-user-notification-missed"
        for message in [
            _notification("turn/started", turn_id, turn={"id": turn_id}),
            # The wrapper attached after app-server had already emitted the
            # userMessage item. Persisted rollout state still has the exact row.
            _notification("item/completed", turn_id, item={
                "id": "cli-answer", "type": "agentMessage",
                "text": "answer", "phase": "final_answer",
            }),
            _notification("turn/completed", turn_id, turn={
                "id": turn_id, "status": "completed",
            }),
        ]:
            await handle._dispatch(message)

        task = ctx.codex_spontaneous_task
        assert task is not None
        await asyncio.wait_for(task, timeout=1)

        users = [event for event in transport.sent
                 if isinstance(event, UserMsg)]
        assert [(event.msg_id, event.prompt) for event in users] == [
            ("persisted-cli-user", "persisted CLI prompt"),
        ]
        assert not any(event.prompt == "" for event in users)
        assert [
            (event.msg_id, event.turn_id)
            for event in transport.sent
            if isinstance(event, TurnBinding)
        ] == [(turn_id, turn_id), ("persisted-cli-user", turn_id)]
        assert recoveries == [(
            ctx.session_id,
            turn_id,
            turn_id,
            0,
            {
                "max_reverse_scan_bytes": (
                    machine.CODEX_LIVE_USER_RECOVERY_SCAN_BYTES
                ),
            },
        )]
        assert refreshes == [(
            ctx.session_id,
            {
                "before": None,
                "limit": machine.MIRROR_LIMIT,
                "cwd": None,
                "detail": "summary",
            },
        )]

    asyncio.run(run())


def test_machine_refreshes_history_after_missed_cli_user_recovery(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        turn_id = "cli-user-persisted-late"
        ctx.codex_spontaneous_turn_id = turn_id
        refreshes = []

        class Sdk:
            async def receive_spontaneous_response(self, requested_turn_id):
                assert requested_turn_id == turn_id
                yield _notification("item/completed", turn_id, item={
                    "id": "cli-answer", "type": "agentMessage",
                    "text": "answer", "phase": "final_answer",
                })
                yield _notification("turn/completed", turn_id, turn={
                    "id": turn_id, "status": "completed",
                })

        async def recover(*_args, **_kwargs):
            return None

        ctx.sdk = Sdk()
        machine.sessions[ctx.key] = ctx
        monkeypatch.setattr(
            machine, "_recover_official_codex_user", recover)
        monkeypatch.setattr(
            machine,
            "_schedule_history_refresh",
            lambda sid, **kwargs: refreshes.append((sid, kwargs)),
        )

        await machine._run_codex_spontaneous_turn(
            ctx, turn_id, announce_running=False)

        users = [event for event in transport.sent
                 if isinstance(event, UserMsg)]
        assert [(event.msg_id, event.prompt) for event in users] == [
            (turn_id, ""),
        ]
        assert refreshes == [(
            ctx.session_id,
            {
                "before": None,
                "limit": machine.MIRROR_LIMIT,
                "cwd": None,
                "detail": "summary",
            },
        )]

    asyncio.run(run())


def test_machine_projects_later_cli_user_item_as_turn_steer():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        handle._using_daemon_proxy = True
        handle._daemon_proxy_established = True
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.turn_lifecycle_callback = (
            lambda phase, turn_id: machine._on_codex_turn_lifecycle(
                ctx, phase, turn_id))

        turn_id = "cli-steered-turn"
        for message in [
            _notification("turn/started", turn_id, turn={"id": turn_id}),
            _notification("item/completed", turn_id, item={
                "id": "cli-first", "type": "userMessage",
                "text": "first CLI prompt",
            }),
            _notification("item/completed", turn_id, item={
                "id": "answer-before-steer", "type": "agentMessage",
                "text": "working", "phase": "commentary",
            }),
        ]:
            await handle._dispatch(message)

        await handle._dispatch(_notification(
            "item/completed", turn_id, item={
                "id": "cli-second", "type": "userMessage",
                "text": "second CLI prompt",
            }))

        async def wait_for_second_user():
            while not any(
                isinstance(event, TurnSteered)
                and event.msg_id == "cli-second"
                for event in transport.sent
            ):
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_second_user(), timeout=1)
        lease = machine._codex_turn_leases.get(ctx.key)
        assert lease is not None
        assert lease.turn_id == turn_id
        assert lease.msg_id == "cli-second"
        assert lease.automatic is True

        await handle._dispatch(_notification(
            "turn/completed", turn_id, turn={
                "id": turn_id, "status": "completed",
            }))

        task = ctx.codex_spontaneous_task
        assert task is not None
        await asyncio.wait_for(task, timeout=1)

        users = [event for event in transport.sent
                 if isinstance(event, UserMsg)]
        steers = [event for event in transport.sent
                  if isinstance(event, TurnSteered)]
        assert [(event.msg_id, event.prompt) for event in users] == [
            ("cli-first", "first CLI prompt"),
        ]
        assert [(event.msg_id, event.turn_id, event.prompt)
                for event in steers] == [
            ("cli-second", turn_id, "second CLI prompt"),
        ]

    asyncio.run(run())


@pytest.mark.parametrize("outcome_unknown", [False, True])
def test_machine_reconciles_remote_steer_user_item_without_second_steer(
    outcome_unknown,
):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        handle._using_daemon_proxy = True
        handle._daemon_proxy_established = True
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.turn_lifecycle_callback = (
            lambda phase, turn_id: machine._on_codex_turn_lifecycle(
                ctx, phase, turn_id))

        turn_id = "cli-remote-steered-turn"
        await handle._dispatch(
            _notification("turn/started", turn_id, turn={"id": turn_id}))
        await handle._dispatch(_notification(
            "item/completed", turn_id, item={
                "id": "cli-first", "type": "userMessage",
                "text": "first CLI prompt",
            }))

        async def wait_for_first_user():
            while not any(
                isinstance(event, UserMsg) and event.msg_id == "cli-first"
                for event in transport.sent
            ):
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_first_user(), timeout=1)

        async def steer(
            prompt, images=None, *, client_user_message_id=None,
        ):
            assert prompt == "guide from Remote"
            assert images == []
            assert client_user_message_id == "remote-steer"
            if outcome_unknown:
                raise codex_handle_module.CodexSteerOutcomeUnknown(
                    "response lost")
            return turn_id

        handle.steer = steer
        result = await machine._handle_steer(Steer(
            sid=ctx.key,
            cmd_id="remote-steer-command",
            client_id="browser-1",
            msg_id="remote-steer",
            prompt="guide from Remote",
        ))
        assert isinstance(result, Error if outcome_unknown else TurnSteered)
        lease = machine._codex_turn_leases.get(ctx.key)
        assert lease is not None
        assert lease.msg_id == (
            "cli-first" if outcome_unknown else "remote-steer"
        )

        official_user = {
            "id": "official-remote-steer",
            "clientId": "remote-steer",
            "type": "userMessage",
            "content": [{"type": "text", "text": "guide from Remote"}],
        }
        await handle._dispatch(
            _notification("item/started", turn_id, item=official_user))

        async def wait_for_remote_lease():
            while True:
                current = machine._codex_turn_leases.get(ctx.key)
                if current is not None and current.msg_id == "remote-steer":
                    return
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_remote_lease(), timeout=1)
        for message in [
            _notification("item/completed", turn_id, item=official_user),
            _notification("turn/completed", turn_id, turn={
                "id": turn_id, "status": "completed",
            }),
        ]:
            await handle._dispatch(message)

        task = ctx.codex_spontaneous_task
        assert task is not None
        await asyncio.wait_for(task, timeout=1)

        steers = [event for event in transport.sent
                  if isinstance(event, TurnSteered)]
        assert [(event.msg_id, event.prompt) for event in steers] == [
            ("remote-steer", "guide from Remote"),
        ]
        aliases = [
            event for event in transport.sent
            if isinstance(event, UserMsg)
            and event.msg_id == "official-remote-steer"
        ]
        assert len(aliases) == 1
        assert aliases[0].client_msg_id == "remote-steer"
        assert any(
            isinstance(event, TurnBinding)
            and event.msg_id == "remote-steer"
            and event.turn_id == turn_id
            for event in transport.sent
        )
        assert ctx.codex_published_steers == {}

    asyncio.run(run())


def test_foreign_cli_thread_started_is_only_a_catalog_hint():
    async def run():
        hints = []
        handle = CodexHandle(
            _Cfg(), thread_started_callback=hints.append)
        handle._using_daemon_proxy = True
        handle.thread_id = "thread-current"

        await handle._dispatch({
            "method": "thread/started",
            "params": {"thread": {
                "id": "thread-new-cli",
                "source": "cli",
            }},
        })
        await handle._dispatch({
            "method": "thread/started",
            "params": {"thread": {
                "id": "thread-private-app",
                "source": "appServer",
            }},
        })

        assert hints == ["thread-new-cli"]
        assert handle.thread_id == "thread-current"
        assert handle.turn_id is None
        assert handle.turn_active is False

    asyncio.run(run())


def test_machine_deduplicates_cli_thread_catalog_hints_across_handles():
    async def run():
        machine, transport = _mk_machine()
        first = _mk_ctx("thread-first", "thread-first")
        second = _mk_ctx("thread-second", "thread-second")
        first.engine = second.engine = "codex"
        machine.sessions[first.key] = first
        machine.sessions[second.key] = second
        machine.focused_sid = first.key

        machine._on_codex_thread_started_hint(first, "thread-new-cli")
        machine._on_codex_thread_started_hint(second, "thread-new-cli")
        tasks = tuple(machine._codex_catalog_hint_tasks)
        assert len(tasks) == 1
        await asyncio.gather(*tasks)

        invalidations = [
            event for event in transport.sent
            if isinstance(event, SessionListInvalidated)
        ]
        assert [(event.engine, event.space) for event in invalidations] == [
            ("codex", "code"),
        ]
        assert machine._codex_session_list_epoch == 1
        assert machine.focused_sid == first.key
        assert first.state == second.state == "idle"

    asyncio.run(run())


def test_goal_objective_before_turn_start_becomes_the_user_prompt_once():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.goal_callback = lambda goal: machine._on_codex_goal(ctx, goal)
        handle.turn_lifecycle_callback = (
            lambda phase, turn_id: machine._on_codex_turn_lifecycle(
                ctx, phase, turn_id))

        turn_id = "goal-before-start"
        goal = _goal_notification("证明泰勒展开", turn_id=turn_id)
        await handle._dispatch(goal)
        # A retried native notification must not enqueue the objective twice.
        await handle._dispatch(goal)
        await handle._dispatch(_notification(
            "turn/started", turn_id, turn={"id": turn_id},
        ))
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "completed"},
        ))

        task = ctx.codex_spontaneous_task
        assert task is not None
        await asyncio.wait_for(task, timeout=1)
        anchors = [
            event for event in transport.sent
            if isinstance(event, UserMsg) and event.prompt
        ]
        assert [(event.msg_id, event.prompt) for event in anchors] == [
            (turn_id, "证明泰勒展开"),
        ]
        assert any(isinstance(event, GoalState) for event in transport.sent)

    asyncio.run(run())


def test_goal_objective_after_turn_start_patches_the_empty_anchor_once():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.goal_callback = lambda goal: machine._on_codex_goal(ctx, goal)
        handle.turn_lifecycle_callback = (
            lambda phase, turn_id: machine._on_codex_turn_lifecycle(
                ctx, phase, turn_id))

        turn_id = "goal-after-start"
        await handle._dispatch(_notification(
            "turn/started", turn_id, turn={"id": turn_id},
        ))
        await asyncio.sleep(0)
        goal = _goal_notification("证明余项收敛", turn_id=turn_id)
        await handle._dispatch(goal)
        await handle._dispatch(goal)
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "completed"},
        ))

        task = ctx.codex_spontaneous_task
        assert task is not None
        await asyncio.wait_for(task, timeout=1)
        anchors = [
            event for event in transport.sent
            if isinstance(event, UserMsg) and event.prompt
        ]
        assert [(event.msg_id, event.prompt) for event in anchors] == [
            (turn_id, "证明余项收敛"),
        ]

    asyncio.run(run())


def test_goal_status_only_continuation_keeps_an_empty_anchor():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        handle._goal_objective_baseline = "既有目标"
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.goal_callback = lambda goal: machine._on_codex_goal(ctx, goal)
        handle.turn_lifecycle_callback = (
            lambda phase, turn_id: machine._on_codex_turn_lifecycle(
                ctx, phase, turn_id))

        turn_id = "goal-resume"
        await handle._dispatch(_goal_notification(
            "既有目标", turn_id=turn_id, created_at=1, updated_at=2,
        ))
        await handle._dispatch(_notification(
            "turn/started", turn_id, turn={"id": turn_id},
        ))
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "completed"},
        ))

        task = ctx.codex_spontaneous_task
        assert task is not None
        await asyncio.wait_for(task, timeout=1)
        anchors = [event for event in transport.sent
                   if isinstance(event, UserMsg)]
        assert [(event.msg_id, event.prompt) for event in anchors] == [
            (turn_id, ""),
        ]

    asyncio.run(run())


def test_goal_modify_after_reconnect_primes_baseline_before_set():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        requests = []

        async def request(method, params=None):
            requests.append((method, params))
            if method == "thread/goal/get":
                return {"goal": {
                    "threadId": handle.thread_id,
                    "objective": "旧目标",
                    "status": "paused",
                    "tokensUsed": 10,
                    "timeUsedSeconds": 20,
                    "createdAt": 1,
                    "updatedAt": 2,
                }}
            if method == "thread/goal/set":
                goal = {
                    "threadId": handle.thread_id,
                    "objective": "新目标",
                    "status": "active",
                    "tokensUsed": 10,
                    "timeUsedSeconds": 20,
                    "createdAt": 1,
                    "updatedAt": 3,
                }
                await handle._dispatch(_goal_notification(
                    "新目标", turn_id=None, created_at=1, updated_at=3,
                ))
                await handle._dispatch(_notification(
                    "turn/started", "goal-modified",
                    turn={"id": "goal-modified"},
                ))
                return {"goal": goal}
            raise AssertionError(method)

        handle._request = request
        await handle.set_goal(objective="新目标", status="active")

        assert requests == [
            ("thread/goal/get", {"threadId": handle.thread_id}),
            ("thread/goal/set", {
                "threadId": handle.thread_id,
                "objective": "新目标",
                "status": "active",
            }),
        ]
        assert handle.take_goal_prompt("goal-modified") == "新目标"

    asyncio.run(run())


def test_goal_resume_reuses_paused_objective_for_spontaneous_turn():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        handle.proc = SimpleNamespace(returncode=None)
        requests = []
        paused_goal = {
            "threadId": handle.thread_id,
            "objective": "继续证明泰勒展开",
            "status": "paused",
            "tokensUsed": 10,
            "timeUsedSeconds": 20,
            "createdAt": 1,
            "updatedAt": 2,
        }

        async def request(method, params=None):
            requests.append((method, params))
            if method == "thread/goal/get":
                return {"goal": paused_goal}
            assert method == "thread/goal/set"
            assert params == {
                "threadId": handle.thread_id,
                "status": "active",
            }
            await handle._dispatch(_goal_notification(
                "继续证明泰勒展开",
                turn_id=None,
                status="active",
                created_at=1,
                updated_at=3,
            ))
            await handle._dispatch(_notification(
                "turn/started", "goal-resumed",
                turn={"id": "goal-resumed"},
            ))
            return {"goal": {
                "threadId": handle.thread_id,
                "objective": "继续证明泰勒展开",
                "status": "active",
                "tokensUsed": 10,
                "timeUsedSeconds": 20,
                "createdAt": 1,
                "updatedAt": 3,
            }}

        handle._request = request
        await handle.set_goal(status="active")

        assert requests == [
            ("thread/goal/get", {"threadId": handle.thread_id}),
            ("thread/goal/set", {
                "threadId": handle.thread_id,
                "status": "active",
            }),
        ]
        assert handle.take_goal_prompt("goal-resumed") == "继续证明泰勒展开"

    asyncio.run(run())


def test_unbound_goal_prompt_never_binds_a_managed_user_turn():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        handle.proc = SimpleNamespace(returncode=None)
        handle._goal_objective_baseline = "旧目标"
        handle._goal_baseline_loaded = True

        await handle._dispatch(_goal_notification(
            "新目标", turn_id=None, created_at=1, updated_at=2,
        ))
        assert handle._goal_prompt_candidate is not None
        assert handle._goal_prompt_candidate.objective == "新目标"

        # query() claims turn_active before the authoritative notification.  A
        # stale Goal candidate must be discarded instead of stealing this turn.
        handle.turn_active = True
        await handle._dispatch(_notification(
            "turn/started", "ordinary-turn",
            turn={"id": "ordinary-turn"},
        ))
        assert handle.take_goal_prompt("ordinary-turn") is None
        assert handle._goal_prompt_candidate is None

    asyncio.run(run())


def test_goal_prompt_candidate_expires_before_spontaneous_turn():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        handle.proc = SimpleNamespace(returncode=None)
        handle._goal_objective_baseline = "旧目标"
        handle._goal_status_baseline = "paused"
        handle._goal_baseline_loaded = True

        await handle._dispatch(_goal_notification(
            "新目标", turn_id=None, created_at=1, updated_at=2,
        ))
        assert handle._goal_prompt_candidate is not None
        handle._goal_prompt_candidate.deadline = 0
        await handle._dispatch(_notification(
            "turn/started", "late-goal-turn",
            turn={"id": "late-goal-turn"},
        ))

        assert handle.take_goal_prompt("late-goal-turn") is None
        assert handle._goal_prompt_candidate is None

    asyncio.run(run())


def test_goal_prompt_candidate_cannot_cross_app_server_generation():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        handle.proc = SimpleNamespace(returncode=None)
        handle._goal_objective_baseline = "旧目标"
        handle._goal_status_baseline = "paused"
        handle._goal_baseline_loaded = True

        await handle._dispatch(_goal_notification(
            "新目标", turn_id=None, created_at=1, updated_at=2,
        ))
        assert handle._goal_prompt_candidate is not None
        handle._generation += 1
        await handle._dispatch(_notification(
            "turn/started", "next-generation-turn",
            turn={"id": "next-generation-turn"},
        ))

        assert handle.take_goal_prompt("next-generation-turn") is None
        assert handle._goal_prompt_candidate is None

    asyncio.run(run())


def test_repeated_active_goal_update_binds_one_prompt_only():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        handle.proc = SimpleNamespace(returncode=None)
        handle._goal_objective_baseline = "旧目标"
        handle._goal_status_baseline = "paused"
        handle._goal_baseline_loaded = True
        update = _goal_notification(
            "新目标", turn_id=None, created_at=1, updated_at=2,
        )

        await handle._dispatch(update)
        await handle._dispatch(update)
        await handle._dispatch(_notification(
            "turn/started", "one-goal-turn",
            turn={"id": "one-goal-turn"},
        ))

        assert handle.take_goal_prompt("one-goal-turn") == "新目标"
        assert handle.take_goal_prompt("one-goal-turn") is None
        assert handle._goal_prompt_candidate is None

    asyncio.run(run())


def test_managed_query_clears_unbound_goal_prompt_candidate():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        handle.proc = SimpleNamespace(returncode=None)
        handle._goal_objective_baseline = "旧目标"
        handle._goal_status_baseline = "paused"
        handle._goal_baseline_loaded = True
        await handle._dispatch(_goal_notification(
            "新目标", turn_id=None, created_at=1, updated_at=2,
        ))
        assert handle._goal_prompt_candidate is not None

        async def request(method, params=None):
            assert method == "turn/start"
            return {"turn": {"id": "managed-turn"}}

        handle._request = request
        assert await handle.query("ordinary prompt") == "managed-turn"
        assert handle._goal_prompt_candidate is None

    asyncio.run(run())


def test_failed_goal_resume_clears_prompt_candidate():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        handle.proc = SimpleNamespace(returncode=None)
        handle._goal_objective_baseline = "继续证明泰勒展开"
        handle._goal_status_baseline = "paused"
        handle._goal_baseline_loaded = True

        async def request(method, params=None):
            raise RuntimeError("goal set failed")

        handle._request = request
        with pytest.raises(RuntimeError, match="goal set failed"):
            await handle.set_goal(status="active")
        assert handle._goal_prompt_candidate is None

    asyncio.run(run())


def test_spontaneous_overflow_preserves_authoritative_success_terminal():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-overflow", "thread-overflow")
        ctx.engine = "codex"
        ctx.state = "running"
        turn_id = "auto-overflow-success"
        ctx.codex_spontaneous_turn_id = turn_id

        class OverflowSdk:
            async def receive_spontaneous_response(self, requested_turn_id):
                assert requested_turn_id == turn_id
                yield CodexSpontaneousOverflow(turn_id)
                yield _notification(
                    "item/started", turn_id,
                    item={"id": "after-gap", "type": "agentMessage"},
                )
                yield _notification(
                    "item/agentMessage/delta", turn_id,
                    itemId="after-gap", delta="still streaming",
                )
                yield _notification(
                    "item/completed", turn_id,
                    item={
                        "id": "after-gap",
                        "type": "agentMessage",
                        "text": "still streaming",
                    },
                )
                yield _notification(
                    "turn/completed", turn_id,
                    turn={"id": turn_id, "status": "completed"},
                )

        ctx.sdk = OverflowSdk()
        machine.sessions[ctx.key] = ctx
        repairs = []

        async def failed_repair(_sid):
            repairs.append(_sid)
            raise RuntimeError("projection unavailable")

        machine._push_mirrored_history = failed_repair
        await machine._run_codex_spontaneous_turn(
            ctx, turn_id, announce_running=False)

        assert not [event for event in transport.sent
                    if isinstance(event, Error)]
        assert any(
            isinstance(event, Delta) and event.text == "still streaming"
            for event in transport.sent
        )
        terminal = [event for event in transport.sent
                    if isinstance(event, TurnEnd)]
        assert len(terminal) == 1
        assert terminal[0].turn_id == turn_id
        assert terminal[0].result.subtype == "success"
        assert terminal[0].result.is_error is False
        assert repairs == [ctx.session_id]

    asyncio.run(run())
