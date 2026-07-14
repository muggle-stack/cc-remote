"""In-process MCP server exposing cc-remote's agent-facing tools.

Tools:
- `ask_user(question, options)`: ask the user a multiple-choice clarifying
  question. Blocks until the user answers (client renders a question card).
- `set_mode(mode)`: switch cc's permission mode yourself (e.g. "plan" when the
  user wants to plan, "bypassPermissions" to go back to normal). The mode
  change takes effect immediately for the rest of the turn.

The SDK routes `tools/call` to this in-process server (registered via
`ClaudeAgentOptions.mcp_servers={"cc-remote-ask":{"type":"sdk","instance":...}}`).
Handlers delegate to callbacks provided by WrapperMachine.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from mcp import types
from mcp.server import Server

from cc_remote.protocol import (
    ASK_OPTION_DESCRIPTION_MAX_CHARS,
    ASK_OPTION_LABEL_MAX_CHARS,
    ASK_OPTION_MAX_COUNT,
    ASK_OPTION_MIN_COUNT,
    ASK_QUESTION_MAX_CHARS,
)

ASK_USER_TOOL = "ask_user"
SET_MODE_TOOL = "set_mode"
MODES = ["default", "acceptEdits", "plan", "auto", "bypassPermissions"]

# async (question, options) -> answer string
AskCallback = Callable[[str, list[dict[str, str]]], Awaitable[str]]
# async (mode) -> None  (machine: sdk.set_permission_mode + emit Perm)
SetModeCallback = Callable[[str], Awaitable[None]]


def _normalize_ask_arguments(
    arguments: dict[str, Any],
) -> tuple[str | None, list[dict[str, str]] | None, str | None]:
    """Validate model-originated ask payloads before they reach the wire."""
    question = arguments.get("question")
    opts = arguments.get("options")
    if not isinstance(question, str) or not (
        1 <= len(question) <= ASK_QUESTION_MAX_CHARS
    ):
        return None, None, "ask_user rejected: question is missing or too long"
    if not isinstance(opts, list) or not (
        ASK_OPTION_MIN_COUNT <= len(opts) <= ASK_OPTION_MAX_COUNT
    ):
        return None, None, "ask_user rejected: expected 2-5 options"
    options: list[dict[str, str]] = []
    for option in opts:
        if not isinstance(option, dict) or not set(option) <= {"label", "ds"}:
            return None, None, "ask_user rejected: invalid option"
        label = option.get("label")
        description = option.get("ds")
        if not isinstance(label, str) or not (
            1 <= len(label) <= ASK_OPTION_LABEL_MAX_CHARS
        ):
            return None, None, "ask_user rejected: invalid option label"
        if description is not None and (
            not isinstance(description, str)
            or len(description) > ASK_OPTION_DESCRIPTION_MAX_CHARS
        ):
            return None, None, "ask_user rejected: option detail is too long"
        normalized = {"label": label}
        if description:
            normalized["ds"] = description
        options.append(normalized)
    return question, options, None


def make_ask_server(ask: AskCallback, set_mode: SetModeCallback) -> Server:
    """Build an in-process MCP server exposing the ask_user + set_mode tools."""
    server: Server = Server("cc-remote-ask")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=ASK_USER_TOOL,
                description=(
                    "Ask the user a clarifying question with multiple-choice options. "
                    "Use this in plan mode or whenever you need the user to pick between "
                    "approaches before proceeding. The call blocks until the user answers; "
                    "their selected option's label is returned as the tool result. Do NOT "
                    "ask via plain text when you could use this tool."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": ASK_QUESTION_MAX_CHARS,
                            "description": "The question to ask the user.",
                        },
                        "options": {
                            "type": "array",
                            "description": "2-5 selectable options.",
                            "minItems": ASK_OPTION_MIN_COUNT,
                            "maxItems": ASK_OPTION_MAX_COUNT,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": ASK_OPTION_LABEL_MAX_CHARS,
                                        "description": "Short option label.",
                                    },
                                    "ds": {
                                        "type": "string",
                                        "maxLength": ASK_OPTION_DESCRIPTION_MAX_CHARS,
                                        "description": "Optional one-line detail.",
                                    },
                                },
                                "required": ["label"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["question", "options"],
                },
            ),
            types.Tool(
                name=SET_MODE_TOOL,
                description=(
                    "Switch cc's permission mode yourself. Use this when the user expresses "
                    "intent — e.g. 'let's plan this' / 'plan first' -> set_mode('plan'); "
                    "'just do it' / 'go ahead and edit' -> set_mode('bypassPermissions') or "
                    "'acceptEdits'. Takes effect immediately for the rest of the turn. "
                    "Modes: 'default' (ask each time), 'acceptEdits' (auto-accept edits), "
                    "'plan' (read-only, plan first), 'auto', 'bypassPermissions' (skip all)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"mode": {"type": "string", "enum": MODES}},
                    "required": ["mode"],
                },
            ),
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]):
        if name == ASK_USER_TOOL:
            question, options, error = _normalize_ask_arguments(arguments)
            if error is not None:
                return [types.TextContent(type="text", text=error)]
            assert question is not None and options is not None
            answer = await ask(question, options)
            return [types.TextContent(type="text", text=answer or "(no answer)")]
        if name == SET_MODE_TOOL:
            mode = str(arguments.get("mode", ""))
            if mode not in MODES:
                return [types.TextContent(type="text", text=f"unknown mode: {mode}; valid: {MODES}")]
            await set_mode(mode)
            return [types.TextContent(type="text", text=f"permission mode set to {mode}")]
        return [types.TextContent(type="text", text=f"unknown tool: {name}")]

    return server
