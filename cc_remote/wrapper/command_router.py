"""Command-type dispatch for :class:`WrapperMachine`.

The router deliberately owns only the stable ``type -> handler`` decision.
Reliable-command deduplication, scheduling lanes, ownership checks, ACKs and
unexpected-command logging remain lifecycle concerns of ``WrapperMachine``.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any

from cc_remote.protocol import Pong


COMMAND_HANDLER_NAMES = MappingProxyType({
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
})

WORK_MUTATION_COMMANDS = frozenset({
    "create_work_project",
    "delete_work_project",
    "add_work_source",
    "delete_work_source",
    "create_work_plugin",
    "delete_work_plugin",
    "create_work_schedule",
    "delete_work_schedule",
})

UNHANDLED_COMMAND = object()


class CommandRouter:
    """Resolve a parsed command to the machine's existing async handler."""

    def __init__(self, target: Any):
        self._target = target

    async def dispatch(self, command: Any) -> Any:
        command_type = command.type
        if command_type == "hello":
            if getattr(command, "role", None) != "client":
                return UNHANDLED_COMMAND
            return await self._target._handle_client_hello(command)

        if command_type == "ping":
            await self._target._emit_focused(Pong(n=command.n))
            return None

        if command_type in WORK_MUTATION_COMMANDS:
            return await self._target._handle_work_mutation(command)

        handler_name = COMMAND_HANDLER_NAMES.get(command_type)
        if handler_name is None:
            return UNHANDLED_COMMAND
        # Resolve for every dispatch. Tests and embedded consumers intentionally
        # replace selected handlers on a live WrapperMachine instance.
        handler = getattr(self._target, handler_name)
        return await handler(command)
