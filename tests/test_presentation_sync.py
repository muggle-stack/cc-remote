"""Cross-client completion and Goal presentation regressions."""
from __future__ import annotations
from unittest.mock import AsyncMock

import asyncio
import json
from types import SimpleNamespace

from cc_remote.codex_profiles import CodexProfileRegistry
from cc_remote.protocol import (
    AcknowledgeCompletion,
    CommandAck,
    CompletionState,
    DismissGoal,
    GetGoal,
    GoalState,
    Hello,
    TurnEnd,
    TurnResult,
    deserialize,
    serialize,
)
from tests.test_multisession import _mk_ctx, _mk_machine


def _success(
    turn_id: str,
    *,
    checkpoint_id: str | None = None,
) -> TurnEnd:
    return TurnEnd(
        turn_id=turn_id,
        checkpoint_id=checkpoint_id,
        result=TurnResult(
            subtype="success", duration_ms=10, is_error=False
        ),
    )


def _install_two_codex_profiles(machine) -> None:
    state = machine.cfg.state_dir
    machine._codex_profiles = CodexProfileRegistry.from_json(json.dumps({
        "primary": {
            "label": "Primary",
            "home": str(state / "primary-home"),
            "default": True,
        },
        "secondary": {
            "label": "Secondary",
            "home": str(state / "secondary-home"),
        },
    }))
    machine._codex_profiles_explicit = True


def test_presentation_protocol_round_trips_exact_receipt_ids():
    dismiss = deserialize(serialize(DismissGoal(
        sid="session-1",
        goal_id="goal-1",
        client_id="desktop",
        cmd_id="dismiss-1",
    )))
    assert dismiss.type == "dismiss_goal"
    assert dismiss.goal_id == "goal-1"

    acknowledge = deserialize(serialize(AcknowledgeCompletion(
        sid="session-1",
        completion_id="turn-1",
        client_id="desktop",
        cmd_id="ack-1",
    )))
    assert acknowledge.type == "acknowledge_completion"
    assert acknowledge.completion_id == "turn-1"

    state = deserialize(serialize(CompletionState(
        sid="session-1",
        completion_id="turn-1",
        unread=False,
        revision=2,
    )))
    assert state.type == "completion_state"
    assert state.revision == 2


def test_main_completion_acknowledgement_broadcasts_and_seeds_reconnect():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-1", "session-1")
        machine.sessions[ctx.key] = ctx

        await machine._emit(ctx, _success("turn-1"))
        assert [message.type for message in transport.sent[-2:]] == [
            "turn_end", "completion_state",
        ]
        unread = transport.sent[-1]
        assert isinstance(unread, CompletionState)
        assert unread.completion_id == "turn-1"
        assert unread.unread is True
        assert unread.to is None

        await machine._handle_acknowledge_completion(
            AcknowledgeCompletion(
                sid="session-1",
                completion_id="turn-1",
                client_id="desktop",
                cmd_id="ack-1",
            )
        )
        acknowledged = transport.sent[-1]
        assert isinstance(acknowledged, CompletionState)
        assert acknowledged.unread is False
        assert acknowledged.revision > unread.revision
        assert acknowledged.to is None

        transport.sent.clear()
        await machine._handle_client_hello(Hello(
            role="client",
            client_id="phone",
            route_id="phone-route",
            cursors={"session-1": ctx.seq},
            generations={"session-1": machine.instance_id},
        ))
        seeded = next(
            message for message in transport.sent
            if isinstance(message, CompletionState)
        )
        assert seeded.completion_id == "turn-1"
        assert seeded.unread is False
        assert seeded.to == "phone"
        assert seeded.route_id == "phone-route"

    asyncio.run(run())


def test_claude_completion_receipt_prefers_stable_checkpoint_identity():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-1", "session-1")
        machine.sessions[ctx.key] = ctx

        await machine._emit(ctx, _success(
            "main-assistant-before-background",
            checkpoint_id="top-level-user-checkpoint",
        ))

        unread = transport.sent[-1]
        assert isinstance(unread, CompletionState)
        assert unread.completion_id == "top-level-user-checkpoint"
        assert machine._session_presentation.get(
            "claude", "session-1"
        ).completion_id == "top-level-user-checkpoint"

    asyncio.run(run())


def test_codex_steer_segment_does_not_publish_completion_receipt():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-1", "session-1")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx

        await machine._emit(ctx, TurnEnd(
            turn_id="shared-native-task",
            result=TurnResult(
                subtype="steered", duration_ms=0, is_error=False,
            ),
        ))

        assert [message.type for message in transport.sent] == ["turn_end"]
        snapshot = machine._session_presentation.get(
            "codex", "session-1"
        )
        assert snapshot.completion_id is None
        assert snapshot.completion_revision == 0

        await machine._emit(ctx, _success("shared-native-task"))
        assert isinstance(transport.sent[-1], CompletionState)
        assert transport.sent[-1].completion_id == "shared-native-task"

    asyncio.run(run())


def test_stale_completion_ack_never_clears_a_newer_turn():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-1", "session-1")
        machine.sessions[ctx.key] = ctx
        await machine._emit(ctx, _success("turn-1"))
        await machine._emit(ctx, _success("turn-2"))

        await machine._handle_acknowledge_completion(
            AcknowledgeCompletion(
                sid="session-1",
                completion_id="turn-1",
                client_id="slow-browser",
                cmd_id="stale-ack",
            )
        )
        current = transport.sent[-1]
        assert isinstance(current, CompletionState)
        assert current.completion_id == "turn-2"
        assert current.unread is True

    asyncio.run(run())


def test_completion_ack_retry_recomputes_newer_unread_after_lost_ack():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-1", "session-1")
        machine.sessions[ctx.key] = ctx
        await machine._emit(ctx, _success("turn-1"))
        transport.sent.clear()

        acknowledge = AcknowledgeCompletion(
            sid="session-1",
            completion_id="turn-1",
            client_id="desktop",
            cmd_id="ack-turn-1",
        )
        await machine._process_command(acknowledge)
        first = next(
            event for event in transport.sent
            if isinstance(event, CompletionState)
        )
        assert first.completion_id == "turn-1"
        assert first.unread is False
        assert isinstance(transport.sent[-1], CommandAck)

        # Simulate the acknowledgement response being lost, followed by a
        # newer completion whose broadcast is also missed by this browser.
        transport.sent.clear()
        await machine._emit(ctx, _success("turn-2"))
        transport.sent.clear()
        await machine._process_command(acknowledge)

        current = next(
            event for event in transport.sent
            if isinstance(event, CompletionState)
        )
        assert current.completion_id == "turn-2"
        assert current.unread is True
        assert isinstance(transport.sent[-1], CommandAck)
        seen, cached = machine._command_seen("desktop", "ack-turn-1")
        assert seen is True
        assert cached == ()

    asyncio.run(run())


def test_cold_session_completion_can_be_acknowledged_without_resume():
    async def run():
        machine, transport = _mk_machine()
        machine._session_presentation.mark_completion(
            "claude", "cold-session", "turn-1"
        )

        await machine._process_command(AcknowledgeCompletion(
            sid="cold-session",
            completion_id="turn-1",
            client_id="desktop",
            cmd_id="ack-cold-turn-1",
        ))

        acknowledged = next(
            event for event in transport.sent
            if isinstance(event, CompletionState)
        )
        assert acknowledged.sid == "cold-session"
        assert acknowledged.completion_id == "turn-1"
        assert acknowledged.unread is False
        assert isinstance(transport.sent[-1], CommandAck)
        assert machine.sessions == {}

    asyncio.run(run())


def test_cold_session_catalog_carries_durable_completion_receipts(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        machine._session_presentation.mark_completion(
            "codex", "cold-seen", "turn-1")
        machine._session_presentation.acknowledge_completion(
            "codex", "cold-seen", "turn-1"
        )
        machine._session_presentation.mark_completion(
            "codex", "cold-unread", "turn-2")
        monkeypatch.setattr(
            machine, "_prime_codex_sidebar_watches", AsyncMock(side_effect=lambda _raw: None)
        )

        event = await machine._send_codex_session_list(
            SimpleNamespace(
                space="code", client_id="phone", cmd_id="list-cold"
            ),
            [
                {
                    "session_id": session_id,
                    "summary": session_id,
                    "first_prompt": None,
                    "cwd": "/repo",
                    "last_modified": "1",
                    "git_branch": None,
                    "tag": None,
                    "status": "idle",
                    "forked_from_id": None,
                }
                for session_id in ("cold-seen", "cold-unread")
            ],
        )

        seen, unread = event.sessions
        assert seen.completion_id == "turn-1"
        assert seen.completion_unread is False
        assert seen.completion_revision == 2
        assert unread.completion_id == "turn-2"
        assert unread.completion_unread is True
        assert unread.completion_revision == 1

    asyncio.run(run())


def test_codex_catalog_claims_only_unambiguous_v1_receipt(
    monkeypatch,
):
    async def run():
        machine, _ = _mk_machine()
        path = machine._session_presentation.path
        path.write_text(json.dumps({
            "version": 1,
            "sessions": {
                "legacy-native": {
                    "completion_id": "legacy-turn",
                    "completion_unread": True,
                    "completion_revision": 1,
                    "dismissed_goal_id": None,
                    "updated_at": 1,
                },
            },
        }), encoding="utf-8")
        machine._session_presentation = type(
            machine._session_presentation)(path.parent)
        _install_two_codex_profiles(machine)
        monkeypatch.setattr(
            "cc_remote.wrapper.machine.transcript_presence",
            lambda _sid: False,
        )
        monkeypatch.setattr(
            "cc_remote.wrapper.machine.codex_session_presence",
            lambda _sid, **kwargs: str(
                kwargs.get("codex_home", "")
            ).endswith("secondary-home"),
        )
        monkeypatch.setattr(
            machine, "_prime_codex_sidebar_watches", AsyncMock(side_effect=lambda _raw: None))

        event = await machine._send_codex_session_list(
            SimpleNamespace(
                space="code", client_id="phone", cmd_id="legacy-list"
            ),
            [{
                "session_id": "secondary@legacy-native",
                "native_session_id": "legacy-native",
                "codex_profile_id": "secondary",
                "codex_profile_label": "Secondary",
                "summary": "legacy",
                "cwd": "/repo",
                "status": "idle",
            }],
        )

        assert event.sessions[0].completion_id == "legacy-turn"
        assert machine._session_presentation.legacy_ids() == frozenset()
        assert machine._session_presentation.get(
            "codex", "secondary@legacy-native"
        ).completion_id == "legacy-turn"

    asyncio.run(run())


def test_codex_catalog_keeps_v1_receipt_quarantined_when_claude_is_unknown(
    monkeypatch,
):
    async def run():
        machine, _ = _mk_machine()
        path = machine._session_presentation.path
        path.write_text(json.dumps({
            "version": 1,
            "sessions": {
                "legacy-native": {
                    "completion_id": "legacy-turn",
                    "completion_unread": True,
                    "completion_revision": 1,
                    "dismissed_goal_id": None,
                    "updated_at": 1,
                },
            },
        }), encoding="utf-8")
        machine._session_presentation = type(
            machine._session_presentation)(path.parent)
        _install_two_codex_profiles(machine)
        monkeypatch.setattr(
            "cc_remote.wrapper.machine.transcript_presence",
            lambda _sid: None,
        )
        monkeypatch.setattr(
            "cc_remote.wrapper.machine.codex_session_presence",
            lambda _sid, **kwargs: str(
                kwargs.get("codex_home", "")
            ).endswith("secondary-home"),
        )
        monkeypatch.setattr(
            machine, "_prime_codex_sidebar_watches", AsyncMock(side_effect=lambda _raw: None))

        event = await machine._send_codex_session_list(
            SimpleNamespace(
                space="code", client_id="phone", cmd_id="unknown-list"
            ),
            [{
                "session_id": "secondary@legacy-native",
                "native_session_id": "legacy-native",
                "codex_profile_id": "secondary",
                "codex_profile_label": "Secondary",
                "summary": "legacy",
                "cwd": "/repo",
                "status": "idle",
            }],
        )

        assert event.sessions[0].completion_id is None
        assert machine._session_presentation.legacy_ids() == frozenset({
            "legacy-native",
        })

    asyncio.run(run())


def test_codex_catalog_keeps_v1_receipt_quarantined_for_duplicate_profiles(
    monkeypatch,
):
    async def run():
        machine, _ = _mk_machine()
        path = machine._session_presentation.path
        path.write_text(json.dumps({
            "version": 1,
            "sessions": {
                "legacy-native": {
                    "completion_id": "legacy-turn",
                    "completion_unread": True,
                    "completion_revision": 1,
                    "dismissed_goal_id": None,
                    "updated_at": 1,
                },
            },
        }), encoding="utf-8")
        machine._session_presentation = type(
            machine._session_presentation)(path.parent)
        _install_two_codex_profiles(machine)
        monkeypatch.setattr(
            "cc_remote.wrapper.machine.transcript_presence",
            lambda _sid: False,
        )
        monkeypatch.setattr(
            "cc_remote.wrapper.machine.codex_session_presence",
            lambda _sid, **_kwargs: True,
        )
        profiles = list(machine._codex_profiles)
        monkeypatch.setattr(
            machine, "_prime_codex_sidebar_watches", AsyncMock(side_effect=lambda _raw: None))

        await machine._send_codex_session_list(
            SimpleNamespace(
                space="code", client_id="phone", cmd_id="duplicate-list"
            ),
            [{
                "session_id": "legacy-native",
                "native_session_id": "legacy-native",
                "codex_profile_id": profiles[0].id,
                "codex_profile_label": profiles[0].label,
                "summary": "legacy",
                "cwd": "/repo",
                "status": "idle",
            }],
        )

        assert machine._session_presentation.legacy_ids() == frozenset({
            "legacy-native",
        })

    asyncio.run(run())


def test_failed_and_private_btw_turns_do_not_create_shared_receipts():
    async def run():
        machine, transport = _mk_machine()
        main = _mk_ctx("main", "main")
        await machine._emit(main, TurnEnd(
            turn_id="failed-turn",
            result=TurnResult(
                subtype="error_during_execution",
                duration_ms=10,
                is_error=True,
            ),
        ))
        assert not any(
            isinstance(message, CompletionState) for message in transport.sent
        )

        transport.sent.clear()
        btw = _mk_ctx("btw-private", None)
        btw.btw = True
        btw.parent_sid = "main"
        btw.owner_client_id = "owner"
        await machine._emit(btw, _success("btw-turn"))
        assert [message.type for message in transport.sent] == ["turn_end"]
        # Account ownership is broader than an individual page connection:
        # every tab/device signed in as this owner sees the side-chat terminal.
        assert transport.sent[0].to is None
        assert transport.sent[0].owner_id == "owner"

    asyncio.run(run())


class _GoalSdk:
    def __init__(self):
        self.goal = {
            "threadId": "goal-session",
            "objective": "finish the first task",
            "status": "complete",
            "engine": "codex",
            "tokensUsed": 10,
            "timeUsedSeconds": 20,
            "createdAt": 100,
        }

    async def get_goal(self):
        return dict(self.goal)


def test_goal_dismissal_broadcasts_and_cannot_hide_a_replacement():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("goal-session", "goal-session")
        ctx.engine = "codex"
        ctx.sdk = _GoalSdk()
        machine.sessions[ctx.key] = ctx

        initial = await machine._handle_get_goal(GetGoal(
            sid="goal-session", client_id="desktop", cmd_id="goal-read"
        ))
        assert isinstance(initial, GoalState)
        assert initial.goal_id
        assert initial.dismissed is False

        dismissed = await machine._handle_dismiss_goal(DismissGoal(
            sid="goal-session",
            goal_id=initial.goal_id,
            client_id="desktop",
            cmd_id="goal-dismiss",
        ))
        assert isinstance(dismissed, GoalState)
        assert dismissed.dismissed is True
        assert dismissed.to is None

        phone = await machine._handle_get_goal(GetGoal(
            sid="goal-session", client_id="phone", cmd_id="phone-read"
        ))
        assert phone.dismissed is True
        assert phone.goal_id == initial.goal_id

        ctx.sdk.goal = {
            **ctx.sdk.goal,
            "objective": "finish the replacement task",
            "createdAt": 101,
        }
        replacement = await machine._handle_dismiss_goal(DismissGoal(
            sid="goal-session",
            goal_id=initial.goal_id,
            client_id="desktop",
            cmd_id="late-dismiss",
        ))
        assert replacement.goal_id != initial.goal_id
        assert replacement.dismissed is False

    asyncio.run(run())


def test_profiled_codex_receipts_use_the_routed_session_id():
    async def run():
        machine, transport = _mk_machine()
        _install_two_codex_profiles(machine)
        # Exercise the legacy server path without native read-state support.
        machine._codex_rpc_for_profile = AsyncMock(return_value={
            "thread": {"id": "native-session"}})
        wire_sid = "secondary@native-session"
        ctx = _mk_ctx(wire_sid, "native-session")
        ctx.engine = "codex"
        ctx.codex_profile_id = "secondary"
        ctx.sdk = _GoalSdk()
        ctx.sdk.goal["threadId"] = "native-session"
        machine.sessions[ctx.key] = ctx

        await machine._emit(ctx, _success("turn-profiled"))
        assert machine._session_presentation_fields("codex", wire_sid) == {
            "completion_id": "turn-profiled",
            "completion_unread": True,
            "completion_revision": 1,
        }
        assert machine._session_presentation.get(
            "codex", "native-session"
        ).completion_revision == 0

        await machine._handle_acknowledge_completion(
            AcknowledgeCompletion(
                sid=wire_sid,
                completion_id="turn-profiled",
                client_id="desktop",
                cmd_id="ack-profiled",
            )
        )
        assert machine._session_presentation.get(
            "codex", wire_sid
        ).completion_unread is False

        initial = await machine._handle_get_goal(GetGoal(
            sid=wire_sid, client_id="desktop", cmd_id="goal-profiled",
        ))
        assert isinstance(initial, GoalState)
        assert initial.goal_id
        dismissed = await machine._handle_dismiss_goal(DismissGoal(
            sid=wire_sid,
            goal_id=initial.goal_id,
            client_id="desktop",
            cmd_id="dismiss-profiled",
        ))
        assert dismissed.dismissed is True
        assert machine._session_presentation.get(
            "codex", wire_sid
        ).dismissed_goal_id == initial.goal_id
        assert machine._session_presentation.get(
            "codex", "native-session"
        ).dismissed_goal_id is None
        assert all(
            message.sid == wire_sid
            for message in transport.sent
            if isinstance(message, (CompletionState, GoalState))
        )

    asyncio.run(run())


def test_goal_dismissal_retry_recomputes_replacement_after_lost_ack():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("goal-session", "goal-session")
        ctx.engine = "codex"
        ctx.sdk = _GoalSdk()
        machine.sessions[ctx.key] = ctx

        initial = await machine._handle_get_goal(GetGoal(
            sid="goal-session", client_id="desktop", cmd_id="goal-read"
        ))
        assert isinstance(initial, GoalState)
        assert initial.goal_id
        transport.sent.clear()

        dismiss = DismissGoal(
            sid="goal-session",
            goal_id=initial.goal_id,
            client_id="desktop",
            cmd_id="goal-dismiss",
        )
        await machine._process_command(dismiss)
        first = next(
            event for event in transport.sent if isinstance(event, GoalState)
        )
        assert first.goal_id == initial.goal_id
        assert first.dismissed is True
        assert isinstance(transport.sent[-1], CommandAck)

        # Simulate the GoalState and ACK being lost while another browser
        # creates or reveals a replacement Goal before this command retries.
        transport.sent.clear()
        ctx.sdk.goal = {
            **ctx.sdk.goal,
            "objective": "finish the replacement task",
            "createdAt": 101,
        }
        await machine._process_command(dismiss)

        replacement = next(
            event for event in transport.sent if isinstance(event, GoalState)
        )
        assert replacement.goal_id != initial.goal_id
        assert replacement.dismissed is False
        assert isinstance(transport.sent[-1], CommandAck)
        seen, cached = machine._command_seen("desktop", "goal-dismiss")
        assert seen is True
        assert cached == ()

    asyncio.run(run())


def test_goal_without_generation_marker_is_not_globally_dismissible():
    goal = {
        "threadId": "legacy-goal",
        "objective": "repeatable objective",
        "status": "complete",
        "engine": "codex",
        "tokensUsed": 1,
        "timeUsedSeconds": 2,
    }
    machine, _ = _mk_machine()
    assert machine._goal_identity(goal) is None
