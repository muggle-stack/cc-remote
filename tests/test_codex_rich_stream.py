"""Codex app-server rich-event and ownership regressions (no model calls)."""
from __future__ import annotations

import asyncio
import json
import re

from cc_remote.protocol import (
    AssistantMsgStart,
    Delta,
    ProcessEvent,
    ToolDelta,
    ToolResult,
    ToolUse,
    TurnDiff,
    TurnPlan,
)
from cc_remote.wrapper.codex_handle import CodexHandle
from cc_remote.wrapper import codex_stream as codex_stream_module
from cc_remote.wrapper.codex_stream import (
    CodexStreamTranslator,
    _redact_credentials,
    codex_translate_history,
)


class _Cfg:
    tool_result_max = 8_000
    cc_cwd = "/tmp"
    turn_reader_queue_cap = 32


def _feed(translator: CodexStreamTranslator, messages: list[dict]):
    return [event for message in messages for event in translator.feed(message)]


def test_codex_agent_phase_and_public_reasoning_are_separate():
    translator = CodexStreamTranslator(8_000)
    events = _feed(translator, [
        {"method": "item/started", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "item": {"type": "agentMessage", "id": "commentary-1",
                     "text": "", "phase": "commentary"},
        }},
        {"method": "item/agentMessage/delta", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "itemId": "commentary-1", "delta": "先检查代码。",
        }},
        {"method": "item/completed", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "item": {"type": "agentMessage", "id": "commentary-1",
                     "text": "先检查代码。", "phase": "commentary"},
        }},
        {"method": "item/reasoning/summaryPartAdded", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "itemId": "reasoning-1", "summaryIndex": 0,
        }},
        {"method": "item/reasoning/summaryTextDelta", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "itemId": "reasoning-1", "summaryIndex": 0,
            "delta": "公开推理摘要",
        }},
        {"method": "item/reasoning/textDelta", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "itemId": "reasoning-1", "contentIndex": 0,
            "delta": "RAW_REASONING_MUST_NOT_CROSS",
        }},
        {"method": "item/completed", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "item": {"type": "reasoning", "id": "reasoning-1",
                     "summary": ["公开推理摘要"],
                     "content": ["RAW_REASONING_MUST_NOT_CROSS"],
                     "encryptedContent": "ENCRYPTED_REASONING_MUST_NOT_CROSS"},
        }},
        {"method": "item/completed", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "item": {"type": "agentMessage", "id": "answer-1",
                     "text": "最终答案", "phase": "final_answer"},
        }},
    ])

    starts = [event for event in events if isinstance(event, AssistantMsgStart)]
    deltas = [event for event in events if isinstance(event, Delta)]
    reasoning = [event for event in events
                 if isinstance(event, ProcessEvent)
                 and event.kind == "reasoning"]
    assert starts[0].channel == "commentary"
    assert [(event.text, event.channel) for event in deltas] == [
        ("先检查代码。", "commentary"), ("最终答案", "final")]
    assert reasoning[-1].summary == "公开推理摘要"
    wire = "\n".join(event.model_dump_json() for event in events)
    assert "RAW_REASONING_MUST_NOT_CROSS" not in wire
    assert "ENCRYPTED_REASONING_MUST_NOT_CROSS" not in wire


def test_codex_plan_command_file_diff_and_delta_metadata():
    translator = CodexStreamTranslator(8_000)
    events = _feed(translator, [
        {"method": "turn/plan/updated", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "explanation": "执行计划",
            "plan": [
                {"step": "检查", "status": "completed"},
                {"step": "修复", "status": "inProgress"},
            ],
        }},
        {"method": "item/started", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "item": {"type": "commandExecution", "id": "command-1",
                     "command": "pwd", "cwd": "/repo",
                     "status": "inProgress", "commandActions": []},
        }},
        {"method": "item/commandExecution/outputDelta", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "itemId": "command-1", "delta": "/repo\n",
        }},
        {"method": "item/completed", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "item": {"type": "commandExecution", "id": "command-1",
                     "command": "pwd", "cwd": "/repo",
                     "status": "completed", "commandActions": [],
                     "aggregatedOutput": "/repo\n", "exitCode": 0,
                     "durationMs": 12},
        }},
        {"method": "item/started", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "item": {"type": "fileChange", "id": "patch-1",
                     "status": "inProgress", "changes": []},
        }},
        {"method": "item/fileChange/patchUpdated", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "itemId": "patch-1", "changes": [{
                "path": "/repo/a.py", "kind": {"type": "update"},
                "diff": "@@ -1 +1 @@\n-old\n+new",
            }],
        }},
        {"method": "turn/diff/updated", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "diff": "@@ -1 +1 @@\n-old\n+new",
        }},
        {"method": "item/completed", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "item": {"type": "fileChange", "id": "patch-1",
                     "status": "completed", "changes": [{
                         "path": "/repo/a.py", "kind": {"type": "update"},
                         "diff": "@@ -1 +1 @@\n-old\n+new",
                     }]},
        }},
    ])

    plan = next(event for event in events if isinstance(event, TurnPlan))
    assert plan.explanation == "执行计划"
    assert [entry["status"] for entry in plan.plan] == [
        "completed", "inProgress"]
    command = next(event for event in events
                   if isinstance(event, ToolUse)
                   and event.tool_use_id == "command-1")
    assert command.category == "command"
    assert command.input["command"] == "pwd"
    output = next(event for event in events
                  if isinstance(event, ToolDelta)
                  and event.tool_use_id == "command-1")
    assert (output.stream, output.delta) == ("output", "/repo\n")
    result = next(event for event in events
                  if isinstance(event, ToolResult)
                  and event.tool_use_id == "command-1")
    assert (result.status, result.exit_code, result.duration_ms) == (
        "succeeded", 0, 12)
    patch_delta = next(event for event in events
                       if isinstance(event, ToolDelta)
                       and event.tool_use_id == "patch-1")
    assert patch_delta.stream == "diff" and "+new" in patch_delta.delta
    turn_diff = next(event for event in events if isinstance(event, TurnDiff))
    assert "+new" in turn_diff.diff
    patch_result = next(event for event in events
                        if isinstance(event, ToolResult)
                        and event.tool_use_id == "patch-1")
    assert patch_result.status == "succeeded"
    assert patch_result.diff and "+new" in patch_result.diff


def test_codex_live_append_streams_have_cumulative_and_event_budgets():
    translator = CodexStreamTranslator(10_000)
    output_events = _feed(translator, [
        {"method": "item/commandExecution/outputDelta", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "itemId": "command-budget", "delta": "x",
        }}
        for _ in range(1_100)
    ])
    output = [event for event in output_events if isinstance(event, ToolDelta)]
    assert len(output) == 1_024
    assert len("".join(event.delta for event in output)) <= 10_000
    assert "截断" in output[-1].delta

    _feed(translator, [{"method": "item/completed", "params": {
        "threadId": "thread-1", "turnId": "turn-1",
        "item": {"type": "commandExecution", "id": "command-budget",
                 "command": "yes", "status": "completed",
                 "aggregatedOutput": "done", "exitCode": 0},
    }}])
    assert not any(key[0] == "command-budget"
                   for key in translator._delta_chars)
    late = translator.feed({
        "method": "item/commandExecution/outputDelta",
        "params": {"threadId": "thread-1", "turnId": "turn-1",
                   "itemId": "command-budget", "delta": "late"},
    })
    assert not any(isinstance(event, ToolDelta) for event in late)

    process_translator = CodexStreamTranslator(32)
    reasoning = _feed(process_translator, [
        {"method": "item/reasoning/summaryTextDelta", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "itemId": "reasoning-budget", "delta": "r" * 20,
        }}
        for _ in range(4)
    ])
    process_deltas = [
        event.delta for event in reasoning
        if isinstance(event, ProcessEvent) and event.delta
    ]
    assert len("".join(process_deltas)) <= 32
    assert "截断" in process_deltas[-1]
    process_translator.feed({
        "method": "turn/completed",
        "params": {"turn": {"id": "turn-1", "status": "completed"}},
    })
    assert process_translator._delta_chars == {}
    assert process_translator._delta_events == {}


def test_codex_unique_live_items_are_bounded_and_dropped_results_stay_dropped(
    monkeypatch,
):
    monkeypatch.setattr(codex_stream_module, "_MAX_LIVE_ITEMS", 3)
    translator = CodexStreamTranslator(8_000)
    accepted = _feed(translator, [{
        "method": "item/started",
        "params": {"turnId": "turn-1", "item": {
            "type": "commandExecution", "id": f"accepted-{index}",
            "command": "true", "status": "inProgress",
        }},
    } for index in range(3)])
    assert len([event for event in accepted if isinstance(event, ToolUse)]) == 3
    assert len(translator._live_items) == 3

    overflow = translator.feed({
        "method": "item/started",
        "params": {"turnId": "turn-1", "item": {
            "type": "mcpToolCall", "id": "dropped-mcp",
            "server": "private", "tool": "lookup", "status": "inProgress",
            "arguments": {"password": "DROPPED_INPUT_SECRET"},
        }},
    })
    assert len([event for event in overflow
                if isinstance(event, ProcessEvent)
                and event.item_id == codex_stream_module._LIVE_ITEMS_OMITTED_ID]) == 1
    assert not any(isinstance(event, ToolUse) for event in overflow)

    late = translator.feed({
        "method": "item/completed",
        "params": {"turnId": "turn-1", "item": {
            "type": "mcpToolCall", "id": "dropped-mcp",
            "server": "private", "tool": "lookup", "status": "completed",
            "arguments": {"password": "DROPPED_INPUT_SECRET"},
            "result": {"content": [{"type": "text", "text": "hidden"}],
                       "_meta": {"token": "DROPPED_RESULT_SECRET"}},
        }},
    })
    assert late == []
    repeated = translator.feed({
        "method": "item/completed",
        "params": {"turnId": "turn-1", "item": {
            "type": "commandExecution", "id": "another-dropped",
            "command": "echo DROPPED_COMPLETED_SECRET", "status": "completed",
            "aggregatedOutput": "DROPPED_COMPLETED_SECRET",
        }},
    })
    assert repeated == []
    wire = "\n".join(event.model_dump_json() for event in overflow + late + repeated)
    assert "DROPPED_INPUT_SECRET" not in wire
    assert "DROPPED_RESULT_SECRET" not in wire
    assert "DROPPED_COMPLETED_SECRET" not in wire
    assert len(translator._tools_started) <= 3
    assert len(translator._started) <= 3

    completed = translator.feed({
        "method": "item/completed",
        "params": {"turnId": "turn-1", "item": {
            "type": "commandExecution", "id": "accepted-0",
            "command": "true", "status": "completed",
            "aggregatedOutput": "ok", "exitCode": 0,
        }},
    })
    assert any(isinstance(event, ToolResult) for event in completed)


def test_codex_credential_redaction_handles_compound_keys_cycles_and_budget():
    payload = {
        "query": "QUERY_VISIBLE",
        "public_key_id": "PUBLIC_KEY_VISIBLE",
        "keyboard_layout": "KEYBOARD_VISIBLE",
        "monkey_business": "MONKEY_VISIBLE",
        "AWS_SECRET_ACCESS_KEY": "AWS_SECRET_SENTINEL",
        "AWS_ACCESS_KEY_ID": "AWS_ACCESS_KEY_SENTINEL",
        "client_secret_key": "CLIENT_SECRET_SENTINEL",
        "authorization_header": "AUTHORIZATION_SENTINEL",
        "private-key-pem": "PRIVATE_KEY_SENTINEL",
        "cookieJar": "COOKIE_SENTINEL",
        "nested": {"sessionToken": "TOKEN_SENTINEL"},
    }

    safe = _redact_credentials(payload)
    assert safe["query"] == "QUERY_VISIBLE"
    assert safe["public_key_id"] == "PUBLIC_KEY_VISIBLE"
    assert safe["keyboard_layout"] == "KEYBOARD_VISIBLE"
    assert safe["monkey_business"] == "MONKEY_VISIBLE"
    for key in (
        "AWS_SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
        "client_secret_key",
        "authorization_header",
        "private-key-pem",
        "cookieJar",
    ):
        assert safe[key] == "[REDACTED]"
    assert safe["nested"]["sessionToken"] == "[REDACTED]"
    wire = json.dumps(safe)
    for secret in (
        "AWS_SECRET_SENTINEL",
        "AWS_ACCESS_KEY_SENTINEL",
        "CLIENT_SECRET_SENTINEL",
        "AUTHORIZATION_SENTINEL",
        "PRIVATE_KEY_SENTINEL",
        "COOKIE_SENTINEL",
        "TOKEN_SENTINEL",
    ):
        assert secret not in wire

    cycle = {"query": "CYCLE_QUERY_VISIBLE"}
    cycle["self"] = cycle
    assert _redact_credentials(cycle)["self"] == "<cycle omitted>"

    def tree(level: int):
        if level == 0:
            return {"query": "TREE_QUERY_VISIBLE"}
        return [tree(level - 1) for _ in range(8)]

    bounded = json.dumps(_redact_credentials(tree(4)))
    assert "redaction budget exceeded" in bounded
    assert len(bounded) < 128 * 1024


def test_codex_mcp_collaboration_and_hook_are_structured_and_sanitized():
    translator = CodexStreamTranslator(8_000)
    events = _feed(translator, [
        {"method": "item/started", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "item": {"type": "mcpToolCall", "id": "mcp-1",
                     "server": "docs", "tool": "lookup",
                     "status": "inProgress", "arguments": {
                         "q": "sdk", "password": "LIVE_PASSWORD_SECRET",
                         "nested": {"access_token": "LIVE_TOKEN_SECRET"},
                         "AWS_SECRET_ACCESS_KEY": "LIVE_AWS_SECRET",
                         "AWS_ACCESS_KEY_ID": "LIVE_AWS_KEY_ID",
                         "client_secret_key": "LIVE_CLIENT_SECRET",
                         "authorization_header": "LIVE_AUTHORIZATION",
                         "env": {"PRIVATE": "LIVE_ENV_SECRET"},
                     }},
        }},
        {"method": "item/mcpToolCall/progress", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "itemId": "mcp-1", "message": "50%",
        }},
        {"method": "item/completed", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "item": {"type": "mcpToolCall", "id": "mcp-1",
                     "server": "docs", "tool": "lookup",
                     "status": "completed", "arguments": {
                         "q": "sdk", "password": "LIVE_PASSWORD_SECRET",
                         "nested": {"access_token": "LIVE_TOKEN_SECRET"},
                         "AWS_SECRET_ACCESS_KEY": "LIVE_AWS_SECRET",
                         "AWS_ACCESS_KEY_ID": "LIVE_AWS_KEY_ID",
                         "client_secret_key": "LIVE_CLIENT_SECRET",
                         "authorization_header": "LIVE_AUTHORIZATION",
                         "env": {"PRIVATE": "LIVE_ENV_SECRET"},
                     },
                     "result": {"content": [{"type": "text", "text": "ok"}],
                                "structuredContent": {"found": True},
                                "_meta": {"secret": "MCP_META_SECRET"}},
                     "error": None, "durationMs": 22},
        }},
        {"method": "item/started", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "item": {"type": "dynamicToolCall", "id": "dynamic-1",
                     "tool": "render", "namespace": "ui",
                     "status": "inProgress", "arguments": {
                         "query": "chart",
                         "privateKey": "DYNAMIC_INPUT_SECRET",
                     }},
        }},
        {"method": "item/completed", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "item": {"type": "dynamicToolCall", "id": "dynamic-1",
                     "tool": "render", "namespace": "ui",
                     "status": "completed", "success": True,
                     "arguments": {
                         "query": "chart",
                         "privateKey": "DYNAMIC_INPUT_SECRET",
                     },
                     "contentItems": [{
                         "type": "text", "text": "ok",
                         "authorization_header": "DYNAMIC_RESULT_SECRET",
                     }]},
        }},
        {"method": "item/started", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "item": {"type": "collabAgentToolCall", "id": "collab-1",
                     "tool": "spawnAgent", "status": "inProgress",
                     "senderThreadId": "thread-1",
                     "receiverThreadIds": ["child-1"], "prompt": "inspect",
                     "model": None, "reasoningEffort": None,
                     "agentsStates": {"child-1": {
                         "status": "COLLAB_STATUS_SECRET",
                         "authorization_header": "COLLAB_STATE_SECRET",
                     }}},
        }},
        {"method": "item/completed", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "item": {"type": "subAgentActivity", "id": "agent-event-1",
                     "kind": "interrupted", "agentThreadId": "child-1",
                     "agentPath": "/root/child"},
        }},
        {"method": "hook/completed", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "run": {"id": "hook-1", "eventName": "preToolUse",
                    "handlerType": "command", "status": "completed",
                    "durationMs": 4,
                    "statusMessage": "HOOK_STATUS_SECRET",
                    "sourcePath": "/secret/hook.py",
                    "command": "echo HOOK_COMMAND_SECRET",
                    "entries": [{"kind": "feedback",
                                 "text": "HOOK_ENTRY_SECRET"}]},
        }},
        {"method": "item/commandExecution/terminalInteraction", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "itemId": "command-1", "processId": "pty-1",
            "stdin": "TERMINAL_STDIN_SECRET",
        }},
    ])

    mcp = next(event for event in events
               if isinstance(event, ToolUse) and event.tool_use_id == "mcp-1")
    assert (mcp.category, mcp.server, mcp.tool) == (
        "mcp", "docs", "lookup")
    assert mcp.input == {
        "q": "sdk", "password": "[REDACTED]",
        "nested": {"access_token": "[REDACTED]"},
        "AWS_SECRET_ACCESS_KEY": "[REDACTED]",
        "AWS_ACCESS_KEY_ID": "[REDACTED]",
        "client_secret_key": "[REDACTED]",
        "authorization_header": "[REDACTED]",
        "env": "[REDACTED]",
    }
    progress = next(event for event in events
                    if isinstance(event, ToolDelta)
                    and event.tool_use_id == "mcp-1")
    assert (progress.stream, progress.delta) == ("progress", "50%")
    result = next(event for event in events
                  if isinstance(event, ToolResult)
                  and event.tool_use_id == "mcp-1")
    assert result.status == "succeeded" and result.duration_ms == 22
    dynamic = next(event for event in events
                   if isinstance(event, ToolUse)
                   and event.tool_use_id == "dynamic-1")
    assert dynamic.input == {
        "query": "chart", "privateKey": "[REDACTED]", "namespace": "ui"}
    dynamic_result = next(event for event in events
                          if isinstance(event, ToolResult)
                          and event.tool_use_id == "dynamic-1")
    assert "DYNAMIC_RESULT_SECRET" not in dynamic_result.content
    assert "[REDACTED]" in dynamic_result.content
    agents = [event for event in events
              if isinstance(event, ProcessEvent) and event.kind == "agent"]
    assert {event.item_id for event in agents} == {"collab-1", "agent-event-1"}
    collab = next(event for event in agents if event.item_id == "collab-1")
    assert collab.input["agents"]["child-1"] == {"status": "unknown"}
    hook = next(event for event in events
                if isinstance(event, ProcessEvent) and event.kind == "hook")
    assert hook.status == "succeeded" and hook.duration_ms == 4
    terminal = next(event for event in events
                    if isinstance(event, ProcessEvent)
                    and event.kind == "terminal")
    assert terminal.parent_id == "command-1"
    assert terminal.status == "succeeded"
    assert terminal.summary == "已向运行中的终端进程写入输入（内容已隐藏）"
    wire = "\n".join(event.model_dump_json() for event in events)
    for secret in (
        "MCP_META_SECRET", "LIVE_PASSWORD_SECRET", "LIVE_TOKEN_SECRET",
        "LIVE_AWS_SECRET", "LIVE_AWS_KEY_ID", "LIVE_CLIENT_SECRET",
        "LIVE_AUTHORIZATION", "LIVE_ENV_SECRET", "DYNAMIC_INPUT_SECRET",
        "DYNAMIC_RESULT_SECRET", "COLLAB_STATUS_SECRET", "COLLAB_STATE_SECRET",
        "HOOK_STATUS_SECRET", "HOOK_COMMAND_SECRET",
        "HOOK_ENTRY_SECRET", "TERMINAL_STDIN_SECRET", "/secret/hook.py",
    ):
        assert secret not in wire


def test_codex_handle_filters_foreign_thread_and_turn_before_queueing():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-current"
        handle.turn_id = "turn-current"
        handle.turn_active = True
        queue = asyncio.Queue()
        handle._turn_q = queue

        await handle._dispatch({
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-foreign", "turnId": "turn-current",
                       "itemId": "foreign-thread", "delta": "wrong"},
        })
        await handle._dispatch({
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-current", "turnId": "turn-foreign",
                       "itemId": "foreign-turn", "delta": "wrong"},
        })
        await handle._dispatch({
            "method": "turn/completed",
            "params": {"threadId": "thread-current",
                       "turn": {"id": "turn-foreign", "status": "completed"}},
        })
        for method, params in (
            ("model/rerouted", {
                "fromModel": "gpt-old", "toModel": "gpt-safe",
                "reason": "highRiskCyberActivity",
            }),
            ("model/safetyBuffering/updated", {
                "model": "gpt-safe", "useCases": [], "reasons": [],
                "showBufferingUi": True, "fasterModel": None,
            }),
            ("model/verification", {
                "verifications": ["trustedAccessForCyber"],
            }),
        ):
            await handle._dispatch({
                "method": method,
                "params": {
                    "threadId": "thread-current", "turnId": "turn-foreign",
                    **params,
                },
            })
        await handle._dispatch({
            "method": "model/verification",
            "params": {
                "turnId": "turn-current",
                "verifications": ["trustedAccessForCyber"],
            },
        })
        await handle._dispatch({
            "method": "model/verification",
            "params": {
                "threadId": "thread-current",
                "verifications": ["trustedAccessForCyber"],
            },
        })
        assert queue.empty()
        assert handle.turn_active is True
        assert handle.turn_id == "turn-current"

        current = {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-current", "turnId": "turn-current",
                       "itemId": "answer", "delta": "right"},
        }
        await handle._dispatch(current)
        assert queue.get_nowait() == current

        current_model_events = [
            {
                "method": "model/rerouted",
                "params": {
                    "threadId": "thread-current", "turnId": "turn-current",
                    "fromModel": "gpt-old", "toModel": "gpt-safe",
                    "reason": "highRiskCyberActivity",
                },
            },
            {
                "method": "model/safetyBuffering/updated",
                "params": {
                    "threadId": "thread-current", "turnId": "turn-current",
                    "model": "gpt-safe", "useCases": ["cyber"],
                    "reasons": ["review"], "showBufferingUi": True,
                    "fasterModel": None,
                },
            },
            {
                "method": "model/verification",
                "params": {
                    "threadId": "thread-current", "turnId": "turn-current",
                    "verifications": ["trustedAccessForCyber"],
                },
            },
        ]
        for message in current_model_events:
            await handle._dispatch(message)
        assert [queue.get_nowait() for _ in current_model_events] == (
            current_model_events)

        terminal = {
            "method": "turn/completed",
            "params": {"threadId": "thread-current",
                       "turn": {"id": "turn-current", "status": "completed"}},
        }
        await handle._dispatch(terminal)
        assert queue.get_nowait() == terminal
        assert queue.get_nowait() is None
        assert handle.turn_active is False
        assert handle.turn_id is None

    asyncio.run(run())


def test_codex_model_safety_notifications_enter_spontaneous_queue():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-current"
        started = {
            "method": "turn/started",
            "params": {
                "threadId": "thread-current", "turnId": "turn-auto",
                "turn": {"id": "turn-auto"},
            },
        }
        verification = {
            "method": "model/verification",
            "params": {
                "threadId": "thread-current", "turnId": "turn-auto",
                "verifications": ["trustedAccessForCyber"],
            },
        }
        completed = {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-current", "turnId": "turn-auto",
                "turn": {"id": "turn-auto", "status": "completed"},
            },
        }
        for message in (started, verification, completed):
            await handle._dispatch(message)

        queued = [message async for message in
                  handle.receive_spontaneous_response("turn-auto")]
        assert queued == [started, verification, completed]

    asyncio.run(run())


def test_codex_model_safety_events_are_bounded_and_never_change_model_chip():
    translator = CodexStreamTranslator(8_000)
    oversized = "x" * 4_096
    events = _feed(translator, [
        {"method": "model/rerouted", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "fromModel": "gpt-source", "toModel": "gpt-safe",
            "reason": "highRiskCyberActivity",
            "unknownSecret": "REROUTE_UNKNOWN_SECRET",
        }},
        {"method": "model/safetyBuffering/updated", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "model": oversized,
            "useCases": ["trusted-cyber", *([oversized] * 64)],
            "reasons": ["policy-review", *([oversized] * 64)],
            "showBufferingUi": True, "fasterModel": oversized,
            "unknownSecret": "BUFFER_UNKNOWN_SECRET",
        }},
        {"method": "model/safetyBuffering/updated", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "model": oversized,
            "useCases": ["trusted-cyber"], "reasons": ["policy-review"],
            "showBufferingUi": False, "fasterModel": None,
        }},
        {"method": "model/verification", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "verifications": ["trustedAccessForCyber", oversized],
            "unknownSecret": "VERIFICATION_UNKNOWN_SECRET",
        }},
    ])

    assert len(events) == 4
    assert all(isinstance(event, ProcessEvent) for event in events)
    reroute = next(event for event in events if event.kind == "model")
    assert reroute.summary == "gpt-source → gpt-safe"
    assert reroute.detail == "原因：highRiskCyberActivity"

    buffering = [event for event in events
                 if event.kind == "safety" and event.title == "模型安全缓冲"]
    assert len(buffering) == 2
    assert buffering[0].item_id == buffering[1].item_id
    assert (buffering[0].phase, buffering[0].status) == ("start", "running")
    assert (buffering[1].phase, buffering[1].status) == ("end", "succeeded")
    assert len(buffering[0].summary or "") <= 64 * 1024
    assert len(buffering[0].detail or "") <= 16 * 1024

    verification = next(event for event in events
                        if event.title == "模型验证")
    assert "trustedAccessForCyber" in (verification.summary or "")
    assert len(verification.summary or "") <= 16 * 1024
    wire = "\n".join(event.model_dump_json() for event in events)
    assert '"type":"model"' not in wire
    for secret in (
        "REROUTE_UNKNOWN_SECRET", "BUFFER_UNKNOWN_SECRET",
        "VERIFICATION_UNKNOWN_SECRET",
    ):
        assert secret not in wire


def test_codex_history_preserves_phase_tools_and_public_reasoning_once(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "session-rich"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-rich"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "inspect"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "agent_reasoning", "text": "公开摘要"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "response_item",
         "payload": {"type": "reasoning", "id": "reasoning-rich",
                     "summary": [{"type": "summary_text", "text": "公开摘要"}],
                     "encrypted_content": "HISTORY_ENCRYPTED_SECRET"}},
        {"timestamp": "2026-01-01T00:00:05Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "commentary",
                     "message": "先运行命令。"}},
        {"timestamp": "2026-01-01T00:00:06Z", "type": "response_item",
         "payload": {"type": "custom_tool_call", "id": "tool-item-1",
                     "call_id": "call-1", "name": "exec", "input": "pwd",
                     "status": "completed"}},
        {"timestamp": "2026-01-01T00:00:07Z", "type": "response_item",
         "payload": {"type": "custom_tool_call_output", "call_id": "call-1",
                     "output": "Process exited with code 0\n/ repo"}},
        {"timestamp": "2026-01-01T00:00:08Z", "type": "event_msg",
         "payload": {"type": "patch_apply_end", "call_id": "call-patch",
                     "turn_id": "turn-rich", "status": "completed",
                     "success": True, "stdout": "ok", "stderr": "",
                     "changes": {"/repo/a.py": {
                         "type": "update", "move_path": None,
                         "unified_diff": "@@ -1 +1 @@\n-old\n+new"}}}},
        {"timestamp": "2026-01-01T00:00:08.1Z", "type": "response_item",
         "payload": {"type": "function_call", "id": "tool-item-mcp",
                     "call_id": "call-mcp", "name": "mcp__docs__lookup",
                     "arguments": json.dumps({
                         "query": "sdk", "api_key": "HISTORY_API_KEY_SECRET",
                         "nested": {"password": "HISTORY_PASSWORD_SECRET"},
                     })}},
        {"timestamp": "2026-01-01T00:00:08.2Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "call-mcp",
                     "output": json.dumps({
                         "content": [{"type": "text", "text": "ok"}],
                         "structuredContent": {
                             "value": 1,
                             "api_token": "HISTORY_RESULT_TOKEN_SECRET",
                         },
                         "_meta": {"secret": "HISTORY_META_SECRET"},
                     })}},
        # Same call id in the response stream must not create a second card.
        {"timestamp": "2026-01-01T00:00:09Z", "type": "response_item",
         "payload": {"type": "custom_tool_call", "id": "tool-item-2",
                     "call_id": "call-patch", "name": "apply_patch",
                     "input": "*** Begin Patch", "status": "completed"}},
        {"timestamp": "2026-01-01T00:00:10Z", "type": "response_item",
         "payload": {"type": "custom_tool_call_output",
                     "call_id": "call-patch", "output": "ok"}},
        {"timestamp": "2026-01-01T00:00:10.1Z", "type": "response_item",
         "payload": {"type": "function_call", "id": "plan-tool-item",
                     "call_id": "call-plan", "name": "update_plan",
                     "arguments": json.dumps({
                         "explanation": "回放计划",
                         "plan": [
                             {"step": "检查", "status": "completed"},
                             {"step": "修复", "status": "in_progress"},
                         ],
                     })}},
        {"timestamp": "2026-01-01T00:00:10.2Z", "type": "response_item",
         "payload": {"type": "function_call_output",
                     "call_id": "call-plan", "output": "plan updated"}},
        {"timestamp": "2026-01-01T00:00:11Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "final_answer",
                     "message": "完成。"}},
        {"timestamp": "2026-01-01T00:00:12Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-rich",
                     "last_agent_message": "完成。", "duration_ms": 12}},
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))

    first, _ = codex_translate_history(str(rollout), 8_000)
    second, _ = codex_translate_history(str(rollout), 8_000)

    text = [(event.text, event.channel) for event in first
            if isinstance(event, Delta)]
    assert text == [("先运行命令。", "commentary"), ("完成。", "final")]
    reasoning = [event for event in first
                 if isinstance(event, ProcessEvent)
                 and event.kind == "reasoning"]
    assert len(reasoning) == 1 and reasoning[0].summary == "公开摘要"
    uses = [event for event in first if isinstance(event, ToolUse)]
    results = [event for event in first if isinstance(event, ToolResult)]
    assert [event.tool_use_id for event in uses] == [
        "call-1", "call-patch", "call-mcp"]
    assert [event.tool_use_id for event in results] == [
        "call-1", "call-patch", "call-mcp"]
    assert uses[0].category == "command" and uses[0].input["command"] == "pwd"
    assert results[0].exit_code == 0 and results[0].status == "succeeded"
    assert results[1].diff and "+new" in results[1].diff
    mcp = next(event for event in uses if event.tool_use_id == "call-mcp")
    assert mcp.category == "mcp"
    assert mcp.input["api_key"] == "[REDACTED]"
    assert mcp.input["nested"]["password"] == "[REDACTED]"
    mcp_result = next(event for event in results
                      if event.tool_use_id == "call-mcp")
    assert "[REDACTED]" in mcp_result.content
    plans = [event for event in first if isinstance(event, TurnPlan)]
    assert len(plans) == 1
    assert plans[0].explanation == "回放计划"
    assert plans[0].plan == [
        {"step": "检查", "status": "completed"},
        {"step": "修复", "status": "inProgress"},
    ]
    history_wire = "\n".join(event.model_dump_json() for event in first)
    for secret in (
        "HISTORY_ENCRYPTED_SECRET", "HISTORY_API_KEY_SECRET",
        "HISTORY_PASSWORD_SECRET", "HISTORY_RESULT_TOKEN_SECRET",
        "HISTORY_META_SECRET",
    ):
        assert secret not in history_wire
    def identity(events):
        return [
            (event.type, getattr(event, "item_id", None),
             getattr(event, "message_id", None),
             getattr(event, "tool_use_id", None))
            for event in events
        ]
    assert identity(first) == identity(second)


def test_codex_history_prefers_authoritative_legacy_event_shapes(tmp_path):
    """Fixtures mirror persisted Codex 0.144.1 event_msg payloads from nono."""
    rollout = tmp_path / "rollout-authoritative.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "session-authoritative"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-auth"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "run"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "response_item",
         "payload": {"type": "function_call", "id": "exec-item",
                     "call_id": "call-exec", "name": "exec_command",
                     "arguments": json.dumps({"cmd": "echo generic"})}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "call-exec",
                     "output": "generic command output"}},
        {"timestamp": "2026-01-01T00:00:05Z", "type": "event_msg",
         "payload": {
             "type": "exec_command_end", "call_id": "call-exec",
             "command": ["sh", "-lc", "printf authoritative"],
             "cwd": "/repo", "duration": {"secs": 1, "nanos": 500_000_000},
             "exit_code": 7, "aggregated_output": "authoritative command output",
             "formatted_output": "formatted fallback", "stdout": "stdout",
             "stderr": "stderr", "parsed_cmd": [
                 {"type": "read", "path": "/repo/README.md"}],
             "process_id": "proc-1", "source": "unified_exec",
             "status": "failed", "turn_id": "turn-auth",
         }},
        {"timestamp": "2026-01-01T00:00:06Z", "type": "response_item",
         "payload": {"type": "function_call", "id": "mcp-item",
                     "call_id": "call-mcp-auth", "name": "mcp__docs__lookup",
                     "arguments": json.dumps({"query": "generic"})}},
        {"timestamp": "2026-01-01T00:00:07Z", "type": "response_item",
         "payload": {"type": "function_call_output",
                     "call_id": "call-mcp-auth",
                     "output": json.dumps({"content": [
                         {"type": "text", "text": "generic mcp output"}]})}},
        {"timestamp": "2026-01-01T00:00:08Z", "type": "event_msg",
         "payload": {
             "type": "mcp_tool_call_end", "call_id": "call-mcp-auth",
             "duration": {"secs": 2, "nanos": 250_000_000},
             "invocation": {"server": "docs", "tool": "lookup",
                            "arguments": {"query": "sdk",
                                          "password": "AUTH_INPUT_SECRET"}},
             "result": {"Ok": {
                 "content": [{"type": "text", "text": "authoritative mcp"}],
                 "structuredContent": {"value": 2,
                                       "access_token": "AUTH_RESULT_SECRET"},
                 "isError": False,
                 "_meta": {"secret": "AUTH_META_SECRET"},
             }},
         }},
        {"timestamp": "2026-01-01T00:00:09Z", "type": "event_msg",
         "payload": {
             "type": "item_completed", "completed_at_ms": 100,
             "thread_id": "session-authoritative", "turn_id": "turn-auth",
             "item": {"id": "plan-detail-1", "type": "Plan",
                      "text": "1. inspect\n2. repair"},
         }},
        {"timestamp": "2026-01-01T00:00:10Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "final_answer",
                     "message": "done"}},
        {"timestamp": "2026-01-01T00:00:11Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-auth",
                     "last_agent_message": "done", "duration_ms": 11}},
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(str(rollout), 8_000)
    uses = [event for event in events if isinstance(event, ToolUse)]
    results = [event for event in events if isinstance(event, ToolResult)]
    assert [event.tool_use_id for event in uses] == [
        "call-exec", "call-mcp-auth"]
    assert [event.tool_use_id for event in results] == [
        "call-exec", "call-mcp-auth"]

    command = uses[0]
    assert command.input["command"] == "sh -lc 'printf authoritative'"
    assert command.input["cwd"] == "/repo"
    assert command.title == "读取 /repo/README.md"
    command_result = results[0]
    assert command_result.content == "authoritative command output"
    assert (command_result.status, command_result.exit_code,
            command_result.duration_ms, command_result.is_error) == (
                "failed", 7, 1_500, True)

    mcp = uses[1]
    assert (mcp.category, mcp.server, mcp.tool) == (
        "mcp", "docs", "lookup")
    assert mcp.input == {"query": "sdk", "password": "[REDACTED]"}
    mcp_result = results[1]
    assert mcp_result.duration_ms == 2_250
    assert mcp_result.status == "succeeded" and not mcp_result.is_error
    assert "authoritative mcp" in mcp_result.content
    assert "[REDACTED]" in mcp_result.content
    assert "generic mcp output" not in mcp_result.content

    plan = next(event for event in events
                if isinstance(event, ProcessEvent) and event.kind == "plan")
    assert plan.item_id == "plan-detail-1"
    assert plan.turn_id == "turn-auth"
    assert plan.detail == "1. inspect\n2. repair"
    wire = "\n".join(event.model_dump_json() for event in events)
    for secret in ("AUTH_INPUT_SECRET", "AUTH_RESULT_SECRET", "AUTH_META_SECRET"):
        assert secret not in wire


def test_codex_rich_ids_and_output_deltas_are_bounded():
    invalid_id = "not safe/id"
    payloads = [
        {"method": "item/started", "params": {"item": {
            "type": "commandExecution", "id": invalid_id, "command": "x",
            "cwd": "/tmp", "status": "inProgress", "commandActions": []}}},
        {"method": "item/commandExecution/outputDelta", "params": {
            "itemId": invalid_id, "delta": "x" * 10_000}},
        {"method": "item/completed", "params": {"item": {
            "type": "commandExecution", "id": invalid_id, "command": "x",
            "cwd": "/tmp", "status": "completed", "commandActions": [],
            "aggregatedOutput": "ok", "exitCode": 0}}},
    ]
    first = _feed(CodexStreamTranslator(128), payloads)
    second = _feed(CodexStreamTranslator(128), payloads)
    use = next(event for event in first if isinstance(event, ToolUse))
    delta = next(event for event in first if isinstance(event, ToolDelta))
    result = next(event for event in first if isinstance(event, ToolResult))
    assert use.tool_use_id == result.tool_use_id
    assert re.fullmatch(r"[a-f0-9]{32}", use.tool_use_id)
    assert len(delta.delta) == 128
    assert use.tool_use_id == next(
        event.tool_use_id for event in second if isinstance(event, ToolUse))
