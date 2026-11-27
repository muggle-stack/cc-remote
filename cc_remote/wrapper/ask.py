"""In-process MCP server exposing cc-remote's agent-facing tools.

Tools:
- `ask_user(question, options)`: ask the user a clarifying question with
  suggested choices and a custom-text fallback. Blocks until the user answers
  (client renders a question card).
- `ask_user(questions=[...])`: collect 1-4 related questions in one paginated
  form, returning all answers together after the user's final submission.
- `set_mode(mode)`: switch cc's permission mode yourself (e.g. "plan" when the
  user wants to plan, "bypassPermissions" to go back to normal). The mode
  change takes effect immediately for the rest of the turn.

The SDK routes `tools/call` to this in-process server (registered via
`ClaudeAgentOptions.mcp_servers={"cc-remote-ask":{"type":"sdk","instance":...}}`).
Handlers delegate to callbacks provided by WrapperMachine.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Awaitable, Callable

from mcp import types
from mcp.server import Server

from cc_remote.protocol import (
    ASK_OPTION_DESCRIPTION_MAX_CHARS,
    ASK_OPTION_LABEL_MAX_CHARS,
    ASK_OPTION_MAX_COUNT,
    ASK_OPTION_MIN_COUNT,
    ASK_QUESTION_MAX_CHARS,
    AskQuestion,
)
from cc_remote.wrapper.claude_questions import normalize_claude_questions

ASK_USER_TOOL = "ask_user"
SET_MODE_TOOL = "set_mode"
MODES = ["default", "acceptEdits", "plan", "auto", "bypassPermissions"]

# async (question, options) -> selected label or custom answer string
AskCallback = Callable[[str, list[dict[str, str]]], Awaitable[str]]
# async (mode) -> None  (machine: sdk.set_permission_mode + emit Perm)
SetModeCallback = Callable[[str], Awaitable[None]]
AskBatchCallback = Callable[[list[AskQuestion]], Awaitable[str]]


def add_message_tools(server: Server, messages: Server) -> None:
    """Expose the same caller-bound tools on an old worker's existing MCP route.

    Old persistent services cannot acquire a new SDK server without restarting
    their native child. Their existing ask proxy can still relay tools/list and
    tools/call to this controller, including after an official MCP toggle.
    """
    if getattr(server, "_cc_remote_messages_added", False):
        return
    original_list = server.request_handlers[types.ListToolsRequest]
    original_call = server.request_handlers[types.CallToolRequest]

    async def list_tools(request):
        original = (await original_list(request)).root
        extra = (await messages.request_handlers[types.ListToolsRequest](request)).root
        return types.ServerResult(types.ListToolsResult(tools=[*original.tools, *extra.tools]))

    async def call_tool(request):
        if request.params.name in {"list_sessions", "send_message", "message_status"}:
            return await messages.request_handlers[types.CallToolRequest](request)
        return await original_call(request)

    server.request_handlers[types.ListToolsRequest] = list_tools
    server.request_handlers[types.CallToolRequest] = call_tool
    server._cc_remote_messages_added = True


def _normalize_ask_arguments(
    arguments: dict[str, Any],
) -> tuple[str | None, list[dict[str, str]] | None, str | None]:
    """Validate model-originated ask payloads before they reach the wire."""
    if set(arguments) != {"question", "options"}:
        return None, None, "ask_user rejected: expected question and options"
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


def make_ask_server(
    ask: AskCallback, set_mode: SetModeCallback, *, ask_batch: AskBatchCallback | None = None,
    modes: tuple[str, ...] | None = None,
) -> Server:
    """Build an in-process MCP server exposing the ask_user + set_mode tools."""
    server: Server = Server("cc-remote-ask")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        tools = [
            types.Tool(
                name=ASK_USER_TOOL,
                description=(
                    "Ask the user a clarifying question with suggested options and a "
                    "custom-text fallback. "
                    "Use this in plan mode or whenever you need the user to pick between "
                    "approaches before proceeding. The call blocks until the user answers; "
                    "their selected option label or custom answer is returned as the tool "
                    "result. Do NOT "
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
                ) if modes is None else (
                    "Switch this session's native permission mode when the user requests it. "
                    "Available modes: default (ask per permission settings), accept-edits "
                    "(approve workspace edits), and plan (plan before executing)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"mode": {"type": "string", "enum": MODES if modes is None else list(modes)}},
                    "required": ["mode"],
                },
            ),
        ]
        if ask_batch is not None:
            schema = tools[0].inputSchema
            options = deepcopy(schema["properties"]["options"])
            options["maxItems"] = 4
            options["description"] = "2-4 selectable options."
            properties = options["items"]["properties"]
            properties["description"] = properties.pop("ds")
            schema["properties"]["questions"] = {
                "type": "array", "minItems": 1, "maxItems": 4,
                "description": "Ask 1-4 related questions together; answers are submitted together.",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "question": deepcopy(schema["properties"]["question"]),
                        "header": {"type": "string", "maxLength": 512},
                        "options": options,
                        "multiSelect": {"type": "boolean", "default": False},
                    },
                    "required": ["question", "options"],
                },
            }
            # Claude Code may filter out tools with root combinators instead
            # of normalizing them. Keep both input forms in a plain object;
            # the handler enforces that exactly one complete form is supplied.
            schema.pop("required")
            schema["additionalProperties"] = False
            tools[0].description += (
                " Use questions for a batch of 1-4 related questions, with optional "
                "header and multiSelect. Users can go back and edit before one final "
                "submission. Batch results contain an answers array of question/answer pairs. "
                "Supply exactly one form: questions, or both question and options. "
                "Do not combine questions with the legacy question/options fields."
            )
        return tools

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]):
        if name == ASK_USER_TOOL:
            if "questions" in arguments:
                if ask_batch is None or set(arguments) != {"questions"}:
                    return [types.TextContent(type="text", text="ask_user rejected: invalid batch")]
                try:
                    questions = normalize_claude_questions(arguments)
                except ValueError as exc:
                    return [types.TextContent(type="text", text=f"ask_user rejected: {exc}")]
                answer = await ask_batch([AskQuestion(
                    question=q.question, header=q.header, options=list(q.options),
                    allow_text=True, multi_select=q.multi_select,
                ) for q in questions])
                return [types.TextContent(type="text", text=answer)]
            question, options, error = _normalize_ask_arguments(arguments)
            if error is not None:
                return [types.TextContent(type="text", text=error)]
            assert question is not None and options is not None
            answer = await ask(question, options)
            return [types.TextContent(type="text", text=answer or "(no answer)")]
        if name == SET_MODE_TOOL:
            mode = str(arguments.get("mode", ""))
            allowed = MODES if modes is None else list(modes)
            if mode not in allowed:
                return [types.TextContent(type="text", text=f"unknown mode: {mode}; valid: {allowed}")]
            await set_mode(mode)
            return [types.TextContent(type="text", text=f"permission mode set to {mode}")]
        return [types.TextContent(type="text", text=f"unknown tool: {name}")]

    return server
