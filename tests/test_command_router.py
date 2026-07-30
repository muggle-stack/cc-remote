"""Zero-token characterization tests for wrapper command dispatch."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cc_remote.protocol import Error, Pong
from cc_remote.wrapper.command_router import (
    COMMAND_HANDLER_NAMES,
    UNHANDLED_COMMAND,
    WORK_MUTATION_COMMANDS,
    CommandRouter,
)
from tests.test_multisession import _mk_machine


EXPECTED_COMMAND_HANDLERS = {
    "query": "_handle_query",
    "cancel_queued_query": "_handle_cancel_queued_query",
    "get_queued_query": "_handle_get_queued_query",
    "update_queued_query": "_handle_update_queued_query",
    "steer": "_handle_steer",
    "interrupt": "_handle_interrupt",
    "takeover": "_handle_takeover",
    "set_model": "_handle_set_model",
    "set_effort": "_handle_set_effort",
    "set_service_tier": "_handle_set_service_tier",
    "set_collaboration_mode": "_handle_set_collaboration_mode",
    "open_btw": "_handle_open_btw",
    "close_btw": "_handle_close_btw",
    "set_perm": "_handle_set_perm",
    "get_permission_profiles": "_handle_get_permission_profiles",
    "set_permission_profile": "_handle_set_permission_profile",
    "set_web_search": "_handle_set_web_search",
    "get_context": "_handle_get_context",
    "get_status": "_handle_get_status",
    "get_diff": "_handle_get_diff",
    "get_file_preview": "_handle_get_file_preview",
    "save_markdown": "_handle_save_markdown",
    "get_preview_asset": "_handle_get_preview_asset",
    "get_history": "_handle_get_history",
    "get_turn_detail": "_handle_get_turn_detail",
    "get_history_image": "_handle_get_history_image",
    "get_models": "_handle_get_models",
    "get_engine_capabilities": "_handle_get_engine_capabilities",
    "manage_engine_plugin": "_handle_manage_engine_plugin",
    "manage_engine_skill": "_handle_manage_engine_skill",
    "manage_engine_hook": "_handle_manage_engine_hook",
    "answer_question": "_handle_answer_question",
    "get_goal": "_handle_get_goal",
    "set_goal": "_handle_set_goal",
    "clear_goal": "_handle_clear_goal",
    "list_sessions": "_handle_list_sessions",
    "switch_session": "_handle_switch_session",
    "new_session": "_handle_new_session",
    "list_dir": "_handle_list_dir",
    "rename_session": "_handle_rename_session",
    "archive_session": "_handle_archive_session",
    "pin_session": "_handle_pin_session",
    "delete_work_session": "_handle_delete_work_session",
    "delete_session": "_handle_delete_session",
    "rollback_session": "_handle_rollback_session",
    "compact_session": "_handle_compact_session",
    "start_review": "_handle_start_review",
    "get_work_dashboard": "_handle_get_work_dashboard",
    "get_work_artifacts": "_handle_get_work_artifacts",
    "fork_session": "_handle_fork_session",
    "fork_session_worktree": "_handle_fork_session_worktree",
    "migrate_session": "_handle_migrate_session",
}

EXPECTED_WORK_MUTATIONS = {
    "create_work_project",
    "delete_work_project",
    "add_work_source",
    "delete_work_source",
    "create_work_plugin",
    "delete_work_plugin",
    "create_work_schedule",
    "delete_work_schedule",
}


class _Target:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.emitted: list[object] = []

    def __getattr__(self, name: str):
        if not name.startswith("_handle_"):
            raise AttributeError(name)

        async def handle(command):
            self.calls.append((name, command.type))
            return f"result:{command.type}"

        return handle

    async def _emit_focused(self, event) -> None:
        self.emitted.append(event)


def test_command_router_has_the_characterized_command_surface():
    assert dict(COMMAND_HANDLER_NAMES) == EXPECTED_COMMAND_HANDLERS
    assert set(WORK_MUTATION_COMMANDS) == EXPECTED_WORK_MUTATIONS
    assert not (set(COMMAND_HANDLER_NAMES) & set(WORK_MUTATION_COMMANDS))


@pytest.mark.parametrize(
    ("command_type", "handler_name"),
    sorted(EXPECTED_COMMAND_HANDLERS.items()),
)
def test_command_router_resolves_direct_handlers_at_dispatch_time(
        command_type, handler_name):
    async def run():
        target = _Target()
        router = CommandRouter(target)
        command = SimpleNamespace(type=command_type)

        result = await router.dispatch(command)

        assert result == f"result:{command_type}"
        assert target.calls == [(handler_name, command_type)]

    asyncio.run(run())


@pytest.mark.parametrize("command_type", sorted(EXPECTED_WORK_MUTATIONS))
def test_command_router_groups_work_mutations_without_changing_the_command(
        command_type):
    async def run():
        target = _Target()
        router = CommandRouter(target)
        command = SimpleNamespace(type=command_type)

        result = await router.dispatch(command)

        assert result == f"result:{command_type}"
        assert target.calls == [("_handle_work_mutation", command_type)]

    asyncio.run(run())


def test_command_router_preserves_client_hello_and_ping_special_cases():
    async def run():
        target = _Target()
        router = CommandRouter(target)

        hello_result = await router.dispatch(SimpleNamespace(
            type="hello", role="client"))
        non_client_result = await router.dispatch(SimpleNamespace(
            type="hello", role="wrapper"))
        ping_result = await router.dispatch(SimpleNamespace(type="ping", n=7))

        assert hello_result == "result:hello"
        assert target.calls == [("_handle_client_hello", "hello")]
        assert non_client_result is UNHANDLED_COMMAND
        assert ping_result is None
        assert len(target.emitted) == 1
        assert isinstance(target.emitted[0], Pong)
        assert target.emitted[0].n == 7

    asyncio.run(run())


def test_command_router_returns_sentinel_for_unknown_command():
    async def run():
        target = _Target()
        router = CommandRouter(target)

        result = await router.dispatch(SimpleNamespace(type="future_command"))

        assert result is UNHANDLED_COMMAND
        assert target.calls == []
        assert target.emitted == []

    asyncio.run(run())


def test_machine_rejects_nonowner_btw_before_command_router():
    async def run():
        machine, _ = _mk_machine()
        rejected = Error(
            code="auth",
            message="private session",
            sid="btw-private",
            to="other-client",
        )

        async def reject(_command):
            return rejected

        class ForbiddenRouter:
            async def dispatch(self, _command):
                raise AssertionError("rejected commands must not reach the router")

        machine._reject_nonowner_btw_command = reject
        machine._command_router = ForbiddenRouter()

        result = await machine._handle(SimpleNamespace(
            type="query", sid="btw-private", client_id="other-client"))

        assert result is rejected

    asyncio.run(run())
