"""Zero-token coverage for Claude rich process events and persisted history."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from claude_agent_sdk.types import (
    AssistantMessage,
    HookEventMessage,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from cc_remote.config import WrapperConfig
from cc_remote.protocol import (
    AssistantMsgEnd,
    Delta,
    ProcessEvent,
    ToolDelta,
    ToolResult,
    ToolUse,
    TurnEnd,
    TurnResult,
    UserMsg,
)
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper import stream as stream_module
from cc_remote.wrapper.stream import (
    StreamTranslator,
    merge_subagent_history,
    translate_history,
    translate_subagent_history,
)


def _assistant(content, *, stop_reason=None, uuid=None, parent=None):
    return AssistantMessage(
        content=content,
        model="claude-test",
        stop_reason=stop_reason,
        uuid=uuid,
        parent_tool_use_id=parent,
    )


def _wire_json(events) -> str:
    return "\n".join(event.model_dump_json() for event in events)


def test_live_thinking_stream_is_public_but_signature_is_never_forwarded():
    translator = StreamTranslator(10_000, turn_id="user-turn")
    first = translator.feed(StreamEvent(
        uuid="11111111-1111-4111-8111-111111111111",
        session_id="session-1",
        event={"type": "content_block_delta", "index": 0, "delta": {
            "type": "thinking_delta", "thinking": "inspect safely",
        }},
    ))
    ignored = translator.feed(StreamEvent(
        uuid="11111111-1111-4111-8111-111111111111",
        session_id="session-1",
        event={"type": "content_block_delta", "index": 0, "delta": {
            "type": "signature_delta", "signature": "NEVER-LEAK-SIGNATURE",
        }},
    ))
    final = translator.feed(_assistant([
        ThinkingBlock(thinking="inspect safely", signature="NEVER-LEAK-SIGNATURE"),
        TextBlock(text="I will inspect the file."),
        ToolUseBlock(
            id="tool-edit", name="Edit",
            input={"file_path": "a.py", "old_string": "old\n", "new_string": "new\n"},
        ),
    ], stop_reason="tool_use", uuid="11111111-1111-4111-8111-111111111111"))

    assert ignored == []
    assert [event.channel for event in first if isinstance(event, Delta)] == ["thinking"]
    # The assembled ThinkingBlock confirms/finalizes the streamed block; it must
    # not duplicate already-emitted text.
    all_events = first + final
    assert [event.text for event in all_events if isinstance(event, Delta)].count(
        "inspect safely") == 1
    commentary = [event for event in final if isinstance(event, Delta)]
    assert commentary[0].channel == "commentary"
    tool = next(event for event in final if isinstance(event, ToolUse))
    assert tool.category == "file" and tool.title == "编辑 · a.py"
    assert "NEVER-LEAK-SIGNATURE" not in _wire_json(all_events)

    result = translator.feed(UserMessage(content=[ToolResultBlock(
        tool_use_id="tool-edit", content="updated", is_error=False,
    )]))
    completed = next(event for event in result if isinstance(event, ToolResult))
    assert completed.status == "succeeded"
    assert "-old" in completed.diff and "+new" in completed.diff


def test_text_channel_promotes_streamed_unknown_and_classifies_final_vs_commentary():
    translator = StreamTranslator(10_000)
    streamed = translator.feed(StreamEvent(
        uuid="22222222-2222-4222-8222-222222222222",
        session_id="session-1",
        event={"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "done"}},
    ))
    assembled = translator.feed(_assistant(
        [TextBlock(text="done")], stop_reason="end_turn",
        uuid="22222222-2222-4222-8222-222222222222",
    ))
    assert next(event for event in streamed if isinstance(event, Delta)).channel == "unknown"
    assert not any(isinstance(event, Delta) for event in assembled)
    assert next(event for event in assembled if isinstance(event, AssistantMsgEnd)).channel == "final"

    commentary = translator.feed(_assistant(
        [TextBlock(text="checking")], stop_reason=None,
        uuid="33333333-3333-4333-8333-333333333333",
    ))
    assert next(event for event in commentary if isinstance(event, Delta)).channel == "commentary"


def test_live_result_promotes_last_ambiguous_top_level_text_without_repeating_it():
    translator = StreamTranslator(10_000)
    streamed = translator.feed(StreamEvent(
        uuid="81818181-8181-4181-8181-818181818181",
        session_id="session-1",
        event={"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "real answer"}},
    ))
    assembled = translator.feed(_assistant(
        [TextBlock(text="real answer")], stop_reason=None,
        uuid="81818181-8181-4181-8181-818181818181",
    ))
    terminal = translator.feed(ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=1, session_id="session-1",
    ))

    message_id = next(
        event.message_id for event in streamed if isinstance(event, Delta))
    assert next(
        event for event in assembled if isinstance(event, AssistantMsgEnd)
    ).channel == "commentary"
    correction = next(
        event for event in terminal if isinstance(event, AssistantMsgEnd))
    assert correction.message_id == message_id
    assert correction.channel == "final"
    assert not any(isinstance(event, Delta) for event in terminal)
    assert isinstance(terminal[-1], TurnEnd)


def test_live_ambiguous_commentary_is_not_promoted_after_separate_tool_activity():
    translator = StreamTranslator(10_000)
    commentary = translator.feed(_assistant(
        [TextBlock(text="I will inspect")], stop_reason=None,
        uuid="82828282-8282-4282-8282-828282828282",
    ))
    candidate_mid = next(
        event.message_id for event in commentary if isinstance(event, Delta))
    translator.feed(_assistant([
        ToolUseBlock(
            id="read-after-commentary", name="Read",
            input={"file_path": "README.md"},
        ),
    ], stop_reason="tool_use",
        uuid="83838383-8383-4383-8383-838383838383"))
    translator.feed(UserMessage(content=[ToolResultBlock(
        tool_use_id="read-after-commentary", content="contents",
        is_error=False,
    )]))
    terminal = translator.feed(ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=1, session_id="session-1",
    ))

    assert not any(
        isinstance(event, AssistantMsgEnd)
        and event.message_id == candidate_mid and event.channel == "final"
        for event in terminal)


def test_server_tools_have_semantic_category_and_bounded_result():
    translator = StreamTranslator(4096, turn_id="turn-1")
    events = translator.feed(_assistant([
        ServerToolUseBlock(id="srv-1", name="web_search", input={"query": "Claude SDK"}),
        ServerToolResultBlock(
            tool_use_id="srv-1",
            content={"type": "web_search_tool_result", "results": ["one", "two"]},
        ),
        TextBlock(text="final answer"),
    ], stop_reason="end_turn", uuid="44444444-4444-4444-8444-444444444444"))
    tool = next(event for event in events if isinstance(event, ToolUse))
    assert tool.category == "web_search"
    assert tool.server == "anthropic" and "Claude SDK" in tool.title
    result = next(event for event in events if isinstance(event, ToolResult))
    assert result.status == "succeeded" and "results" in result.content
    assert next(event for event in events if isinstance(event, Delta)).channel == "final"


def test_live_mcp_input_and_structured_metadata_are_redacted():
    translator = StreamTranslator(4096)
    events = translator.feed(_assistant([
        ToolUseBlock(id="mcp-1", name="mcp__private__lookup", input={
            "query": "safe query", "api_token": "TOKEN-SENTINEL",
            "accessToken": "CAMEL-TOKEN-SENTINEL",
            "nested": {"password": "PASSWORD-SENTINEL"},
            "environment": {"AUTH": "ENV-SENTINEL"},
            "deep": {"a": {"b": {"c": {"clientSecret": "DEEP-SENTINEL"}}}},
        }),
    ], stop_reason="tool_use"))
    tool = next(event for event in events if isinstance(event, ToolUse))
    assert tool.category == "mcp" and tool.server == "private"
    assert tool.input["api_token"] == "***"
    assert tool.input["accessToken"] == "***"
    assert tool.input["nested"]["password"] == "***"
    assert tool.input["environment"] == "***"

    result = translator.feed(UserMessage(content=[ToolResultBlock(
        tool_use_id="mcp-1",
        content={
            "content": [{"type": "text", "text": "visible result"}],
            "privateMetadata": {"token": "RESULT-TOKEN-SENTINEL"},
        },
        is_error=False,
    )]))
    assert next(event for event in result if isinstance(event, ToolResult)).content == "visible result"
    wire = _wire_json(events + result)
    for secret in ("TOKEN-SENTINEL", "CAMEL-TOKEN-SENTINEL", "PASSWORD-SENTINEL",
                   "ENV-SENTINEL", "DEEP-SENTINEL",
                   "RESULT-TOKEN-SENTINEL"):
        assert secret not in wire


def test_sensitive_input_redaction_bounds_wide_graphs_and_cycles():
    recursive = {"password": "CYCLE-SECRET"}
    recursive["self"] = recursive
    wide = [{"value": index} for index in range(10_000)]
    redacted = stream_module._redact_sensitive_input({
        "recursive": recursive,
        "wide": wide,
        "apiToken": "ROOT-SECRET",
    })

    wire = json.dumps(redacted)
    assert "CYCLE-SECRET" not in wire and "ROOT-SECRET" not in wire
    assert "<circular reference omitted>" in wire
    assert "items omitted" in wire
    # The traversal budget, rather than the input graph width, controls output.
    assert len(wire) < 20_000


def test_streamed_text_dedup_tracks_only_lengths_not_complete_prefixes():
    translator = StreamTranslator(1_000_000)
    streamed = []
    for _ in range(128):
        streamed.extend(translator.feed(StreamEvent(
            uuid="abababab-abab-4bab-8bab-abababababab",
            session_id="session-1",
            event={"type": "content_block_delta", "index": 0,
                   "delta": {"type": "text_delta", "text": "x" * 1024}},
        )))

    assert translator._emitted["text"] == 128 * 1024
    assert isinstance(translator._emitted["text"], int)
    assembled = translator.feed(_assistant(
        [TextBlock(text="x" * (128 * 1024))], stop_reason="end_turn",
        uuid="abababab-abab-4bab-8bab-abababababab",
    ))
    assert not any(isinstance(event, Delta) for event in assembled)


def test_tool_progress_is_deduplicated_coalesced_and_flushed_before_result(monkeypatch):
    ticks = iter((1.0, 1.01, 1.02, 1.10))
    monkeypatch.setattr(stream_module.time, "monotonic", lambda: next(ticks))
    translator = StreamTranslator(4096)
    translator.feed(_assistant([
        ToolUseBlock(id="bash-1", name="Bash", input={"command": "printf abcdef"}),
    ], stop_reason="tool_use"))

    first = translator.feed(SystemMessage(subtype="bash_progress", data={
        "tool_use_id": "bash-1", "full_output": "abc",
    }))
    buffered = translator.feed(SystemMessage(subtype="bash_progress", data={
        "tool_use_id": "bash-1", "full_output": "abcdef",
    }))
    progress = translator.feed(SystemMessage(subtype="tool_progress", data={
        "tool_use_id": "bash-1", "progress": "running",
    }))
    duplicate = translator.feed(SystemMessage(subtype="tool_progress", data={
        "tool_use_id": "bash-1", "progress": "running",
    }))
    summary = translator.feed(SystemMessage(subtype="tool_use_summary", data={
        "preceding_tool_use_ids": ["bash-1"], "summary": "Ran one command",
    }))
    completed = translator.feed(UserMessage(content=[ToolResultBlock(
        tool_use_id="bash-1", content="abcdef", is_error=False,
    )]))

    assert next(event for event in first if isinstance(event, ToolDelta)).delta == "abc"
    assert buffered == []
    assert [event.stream for event in progress if isinstance(event, ToolDelta)] == [
        "output", "progress"]
    assert progress[0].delta == "def"
    assert duplicate == []
    assert next(event for event in summary if isinstance(event, ToolDelta)).stream == "summary"
    assert not any(isinstance(event, ToolDelta) for event in completed)
    assert isinstance(completed[-1], ToolResult)


def test_tool_delta_burst_is_bounded_and_result_flush_order_is_stable(monkeypatch):
    ticks = iter((1.0, 1.01, 1.02))
    monkeypatch.setattr(stream_module.time, "monotonic", lambda: next(ticks))
    translator = StreamTranslator(8)
    first = translator.feed(SystemMessage(subtype="bash_progress", data={
        "tool_use_id": "bash-1", "full_output": "ab",
    }))
    assert first[0].delta == "ab"
    assert translator.feed(SystemMessage(subtype="bash_progress", data={
        "tool_use_id": "bash-1", "full_output": "abcdefghij",
    })) == []
    assert translator.feed(SystemMessage(subtype="bash_progress", data={
        "tool_use_id": "bash-1", "full_output": "abcdefghijkl",
    })) == []
    terminal = translator.feed(ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=1, session_id="s1",
    ))
    deltas = first + [event for event in terminal if isinstance(event, ToolDelta)]
    assert sum(len(event.delta) for event in deltas) == 8
    assert "".join(event.delta for event in deltas) == "abcdefgh"
    assert isinstance(terminal[-1], TurnEnd)


def test_claude_unique_tools_are_bounded_and_unknown_mcp_results_fail_closed(
    monkeypatch,
):
    monkeypatch.setattr(stream_module, "_MAX_LIVE_TOOL_ITEMS", 2)
    translator = StreamTranslator(8_000, turn_id="turn-1")
    accepted = translator.feed(_assistant([
        ToolUseBlock(id="mcp-accepted", name="mcp__docs__lookup",
                     input={"query": "sdk"}),
        ToolUseBlock(id="bash-accepted", name="Bash",
                     input={"command": "true"}),
    ], stop_reason="tool_use"))
    assert len([event for event in accepted if isinstance(event, ToolUse)]) == 2
    assert len(translator._tool_items) == 2
    assert len(translator._tool_names) == 2

    overflow = translator.feed(_assistant([
        ToolUseBlock(id="mcp-dropped", name="mcp__private__lookup",
                     input={"password": "DROPPED_MCP_INPUT_SECRET"}),
    ], stop_reason="tool_use"))
    assert len([event for event in overflow
                if isinstance(event, ProcessEvent)
                and event.item_id == stream_module._LIVE_TOOL_ITEMS_OMITTED_ID]) == 1
    assert not any(isinstance(event, ToolUse)
                   and event.tool_use_id == "mcp-dropped" for event in overflow)

    dropped_result = translator.feed(UserMessage(content=[ToolResultBlock(
        tool_use_id="mcp-dropped",
        content={
            "content": [{"type": "text", "text": "must stay hidden"}],
            "_meta": {"token": "DROPPED_MCP_RESULT_SECRET"},
        },
        is_error=False,
    )]))
    assert dropped_result == []
    # The fixed full set is the tombstone: another unknown completed id neither
    # allocates state nor emits a second omission marker/result.
    another_dropped = translator.feed(UserMessage(content=[ToolResultBlock(
        tool_use_id="another-dropped",
        content={"_meta": {"token": "ANOTHER_DROPPED_SECRET"}},
        is_error=False,
    )]))
    assert another_dropped == []

    accepted_result = translator.feed(UserMessage(content=[ToolResultBlock(
        tool_use_id="mcp-accepted",
        content={
            "content": [{"type": "text", "text": "visible"}],
            "_meta": {"token": "ACCEPTED_MCP_PRIVATE_META"},
        },
        is_error=False,
    )]))
    result = next(event for event in accepted_result
                  if isinstance(event, ToolResult))
    assert result.content == "visible"
    wire = _wire_json(overflow + dropped_result + another_dropped + accepted_result)
    for secret in (
        "DROPPED_MCP_INPUT_SECRET", "DROPPED_MCP_RESULT_SECRET",
        "ANOTHER_DROPPED_SECRET", "ACCEPTED_MCP_PRIVATE_META",
    ):
        assert secret not in wire
    assert translator.feed(UserMessage(content=[ToolResultBlock(
        tool_use_id="mcp-accepted", content="duplicate", is_error=False,
    )])) == []
    assert len(translator._tool_items) == 2
    assert len(translator._finished_tool_items) == 1


def test_large_edit_diff_bounds_sources_before_difflib_and_output(monkeypatch):
    real_unified_diff = stream_module.difflib.unified_diff
    inspected = {}

    def guarded_unified_diff(old, new, *args, **kwargs):
        inspected["old_chars"] = sum(len(line) for line in old)
        inspected["new_chars"] = sum(len(line) for line in new)
        inspected["old_lines"] = len(old)
        inspected["new_lines"] = len(new)
        assert inspected["old_chars"] <= stream_module._MAX_DIFF_SOURCE_CHARS
        assert inspected["new_chars"] <= stream_module._MAX_DIFF_SOURCE_CHARS
        assert inspected["old_lines"] <= stream_module._MAX_DIFF_SOURCE_LINES
        assert inspected["new_lines"] <= stream_module._MAX_DIFF_SOURCE_LINES
        return real_unified_diff(old, new, *args, **kwargs)

    monkeypatch.setattr(
        stream_module.difflib, "unified_diff", guarded_unified_diff)
    shared = ("same line\n" * 200_000)
    diff, truncated = stream_module._tool_diff("Edit", {
        "file_path": "large.txt",
        "old_string": shared + "old tail\n",
        "new_string": shared + "new tail\n",
    }, 4096)

    assert inspected
    assert diff and len(diff) <= 4096
    assert truncated is True


def test_tool_summary_burst_uses_same_delta_budget(monkeypatch):
    ticks = iter((1.0, 1.01, 1.02))
    monkeypatch.setattr(stream_module.time, "monotonic", lambda: next(ticks))
    translator = StreamTranslator(8)
    emitted = []
    for summary in ("abcd", "efgh", "ijkl"):
        emitted.extend(translator.feed(SystemMessage(
            subtype="tool_use_summary",
            data={"tool_use_id": "tool-1", "summary": summary},
        )))
    emitted.extend(translator.feed(ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=1, session_id="s1",
    )))
    deltas = [event for event in emitted if isinstance(event, ToolDelta)]
    assert sum(len(event.delta) for event in deltas) == 8
    assert all(event.stream == "summary" for event in deltas)


def test_task_updates_keep_origin_turn_across_translator_instances():
    item_turns = {}
    item_titles = {}
    item_meta = {}
    first = StreamTranslator(
        4096, turn_id="old-turn", item_turns=item_turns,
        item_titles=item_titles, item_meta=item_meta)
    started = first.feed(TaskStartedMessage(
        subtype="task_started", data={}, task_id="task-1",
        description="Review backend", uuid="u1", session_id="s1",
        tool_use_id="agent-tool", task_type="agent",
    ))
    assert started[0].turn_id == "old-turn"
    assert started[0].kind == "agent" and started[0].status == "running"

    second = StreamTranslator(
        4096, turn_id="new-turn", item_turns=item_turns,
        item_titles=item_titles, item_meta=item_meta)
    progressed = second.feed(TaskProgressMessage(
        subtype="task_progress", data={}, task_id="task-1",
        description="Review backend",
        usage={"total_tokens": 120, "tool_uses": 2, "duration_ms": 3000},
        uuid="u2", session_id="s1", tool_use_id="agent-tool",
        last_tool_name="Read",
    ))
    updated = second.feed(TaskUpdatedMessage(
        subtype="task_updated", data={}, task_id="task-1",
        patch={"status": "running", "secret": "must-not-forward"},
        status="running", session_id="s1", uuid="u3",
    ))
    ended = second.feed(TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="task-1",
        status="completed", output_file="/private/task-output",
        summary="review complete", uuid="u4", session_id="s1",
        tool_use_id="agent-tool",
    ))
    assert all(event.turn_id == "old-turn" for event in progressed + updated + ended)
    assert all(event.kind == "agent" for event in progressed + updated + ended)
    assert all(event.parent_id == "agent-tool" for event in progressed + updated + ended)
    assert ended[0].status == "succeeded" and ended[0].phase == "end"
    wire = _wire_json(progressed + updated + ended)
    assert "must-not-forward" not in wire and "/private/task-output" not in wire


def test_hook_lifecycle_forwards_only_safe_metadata():
    translator = StreamTranslator(4096, turn_id="turn-1")
    common = {
        "hook_id": "hook-call-1", "tool_use_id": "tool-1",
        "command": "env | curl secret", "environment": {"TOKEN": "TOP-SECRET"},
    }
    started = translator.feed(HookEventMessage(
        subtype="hook_started", data={**common, "output": "TOP-SECRET"},
        hook_event_name="PreToolUse", session_id="s1", uuid="h1",
    ))
    ended = translator.feed(HookEventMessage(
        subtype="hook_response",
        data={**common, "output": "TOP-SECRET", "exit_code": 0,
              "duration_ms": 23, "outcome": "success"},
        hook_event_name="PreToolUse", session_id="s1", uuid="h2",
    ))
    assert started[0].item_id == ended[0].item_id
    assert started[0].status == "running" and ended[0].status == "succeeded"
    assert ended[0].exit_code == 0 and ended[0].duration_ms == 23
    wire = _wire_json(started + ended)
    assert "TOP-SECRET" not in wire and "curl secret" not in wire and "TOKEN" not in wire


def test_history_restores_thinking_channels_semantic_tools_and_plan():
    messages = [
        SimpleNamespace(
            uuid="55555555-5555-4555-8555-555555555555", type="user",
            parent_tool_use_id=None,
            message={"role": "user", "content": "make a plan"},
        ),
        SimpleNamespace(
            uuid="66666666-6666-4666-8666-666666666666", type="assistant",
            parent_tool_use_id=None,
            message={"role": "assistant", "stop_reason": "tool_use", "content": [
                {"type": "thinking", "thinking": "reason", "signature": "HIDDEN-SIG"},
                {"type": "text", "text": "planning"},
                {"type": "tool_use", "id": "plan-enter", "name": "EnterPlanMode", "input": {}},
            ]},
        ),
        SimpleNamespace(
            uuid="77777777-7777-4777-8777-777777777777", type="assistant",
            parent_tool_use_id=None,
            message={"role": "assistant", "stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "plan-exit", "name": "ExitPlanMode",
                 "input": {"plan": "1. inspect\n2. fix"}},
            ]},
        ),
    ]
    events = translate_history(messages, 10_000)
    channels = [event.channel for event in events if isinstance(event, Delta)]
    assert channels == ["thinking", "commentary"]
    assert any(isinstance(event, ProcessEvent) and event.kind == "plan"
               and event.phase == "start" for event in events)
    assert any(event.type == "turn_plan" for event in events)
    assert "HIDDEN-SIG" not in _wire_json(events)


def test_history_promotes_last_ambiguous_top_level_text_at_user_or_eof_boundary():
    base = [
        SimpleNamespace(
            uuid="10101010-1010-4010-8010-101010101010", type="user",
            message={"role": "user", "content": "question"},
        ),
        # Real Claude transcripts can omit stop_reason on the final text row.
        SimpleNamespace(
            uuid="20202020-2020-4020-8020-202020202020", type="assistant",
            parent_tool_use_id=None,
            message={"role": "assistant", "stop_reason": None,
                     "content": [{"type": "text", "text": "real answer"}]},
        ),
    ]
    next_user = SimpleNamespace(
        uuid="30303030-3030-4030-8030-303030303030", type="user",
        message={"role": "user", "content": "next question"},
    )

    for messages in (base, [*base, next_user]):
        answer = next(
            event for event in translate_history(messages, 10_000)
            if isinstance(event, Delta) and event.text == "real answer")
        assert answer.channel == "final"


def test_history_does_not_promote_ambiguous_commentary_before_separate_tool_activity():
    messages = [
        SimpleNamespace(
            uuid="40404040-4040-4040-8040-404040404040", type="user",
            message={"role": "user", "content": "inspect"},
        ),
        SimpleNamespace(
            uuid="50505050-5050-4050-8050-505050505050", type="assistant",
            parent_tool_use_id=None,
            message={"role": "assistant", "stop_reason": None,
                     "content": [{"type": "text", "text": "I will inspect"}]},
        ),
        SimpleNamespace(
            uuid="60606060-6060-4060-8060-606060606060", type="assistant",
            parent_tool_use_id=None,
            message={"role": "assistant", "stop_reason": "tool_use",
                     "content": [{
                         "type": "tool_use", "id": "read-1", "name": "Read",
                         "input": {"file_path": "README.md"},
                     }]},
        ),
        SimpleNamespace(
            uuid="70707070-7070-4070-8070-707070707070", type="user",
            message={"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "read-1",
                "content": "contents",
            }]},
        ),
    ]

    commentary = next(
        event for event in translate_history(messages, 10_000)
        if isinstance(event, Delta) and event.text == "I will inspect")
    assert commentary.channel == "commentary"


def test_subagent_history_is_correlated_nested_and_omits_private_prompt(tmp_path, monkeypatch):
    sid = "88888888-8888-4888-8888-888888888888"
    main = tmp_path / f"{sid}.jsonl"
    rows = [
        {"type": "user", "uuid": "99999999-9999-4999-8999-999999999999",
         "message": {"role": "user", "content": "review it"}},
        {"type": "assistant", "uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
         "message": {"role": "assistant", "content": [{
             "type": "tool_use", "id": "agent-tool", "name": "Agent",
             "input": {"description": "Review backend"},
         }]}},
        {"type": "user", "uuid": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
         "toolUseResult": {"agentId": "agent-123", "status": "completed"},
         "message": {"role": "user", "content": [{
             "type": "tool_result", "tool_use_id": "agent-tool", "content": "done",
         }]}},
    ]
    main.write_text("".join(json.dumps(row) + "\n" for row in rows))
    subdir = tmp_path / sid / "subagents"
    subdir.mkdir(parents=True)
    subrows = [
        {"type": "user", "uuid": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
         "agentId": "agent-123", "timestamp": "2026-07-13T01:00:00Z",
         "message": {"role": "user", "content": "PRIVATE DELEGATED PROMPT"}},
        {"type": "assistant", "uuid": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
         "agentId": "agent-123", "timestamp": "2026-07-13T01:00:01Z",
         "message": {"role": "assistant", "content": [{
             "type": "thinking", "thinking": "inspect", "signature": "PRIVATE-SIGNATURE",
         }]}},
        {"type": "assistant", "uuid": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
         "agentId": "agent-123", "timestamp": "2026-07-13T01:00:02Z",
         "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{
             "type": "text", "text": "review complete",
         }]}},
    ]
    (subdir / "agent-agent-123.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in subrows))
    monkeypatch.setattr(stream_module, "transcript_path", lambda _sid: str(main))

    recovered = translate_subagent_history(sid, 10_000)
    assert isinstance(recovered[0], ProcessEvent) and recovered[0].phase == "start"
    assert isinstance(recovered[-1], ProcessEvent) and recovered[-1].phase == "end"
    assert recovered[0].parent_id == "agent-tool"
    assert [event.channel for event in recovered if isinstance(event, Delta)] == [
        "thinking", "commentary"]
    wire = _wire_json(recovered)
    assert "PRIVATE DELEGATED PROMPT" not in wire
    assert "PRIVATE-SIGNATURE" not in wire

    main_events = [
        UserMsg(msg_id="99999999-9999-4999-8999-999999999999", prompt="review it"),
        ToolUse(message_id="main-msg", tool_use_id="agent-tool", tool="Agent",
                input={}, category="agent"),
        TurnEnd(result=TurnResult(subtype="success", duration_ms=1, is_error=False)),
    ]
    merged = merge_subagent_history(main_events, recovered)
    tool_index = next(i for i, event in enumerate(merged) if isinstance(event, ToolUse))
    assert merged[tool_index + 1] is recovered[0]


def test_sdk_enables_hook_events_explicitly():
    options = SdkHandle(WrapperConfig())._options(None, "/tmp")
    assert options.include_partial_messages is True
    assert options.include_hook_events is True


def test_sdk_single_pump_forwards_post_result_background_events_immediately():
    async def run():
        class Query:
            def __init__(self):
                self.queue = asyncio.Queue()
                self.consumers = 0
                self.progress_read = asyncio.Event()

            async def receive_messages(self):
                self.consumers += 1
                while True:
                    item = await self.queue.get()
                    if item is None:
                        return
                    if (item.get("type") == "system"
                            and item.get("subtype") == "task_progress"):
                        self.progress_read.set()
                    yield item

        class Client:
            def __init__(self, query):
                self._query = query
                self.queries = []

            async def query(self, prompt):
                self.queries.append(prompt)

        def result():
            return {
                "type": "result", "subtype": "success",
                "duration_ms": 1, "duration_api_ms": 1,
                "is_error": False, "num_turns": 1,
                "session_id": "session-1",
            }

        query = Query()
        handle = SdkHandle(SimpleNamespace(turn_reader_queue_cap=2))
        handle.client = Client(query)
        background = []
        delivered = asyncio.Event()

        async def on_background(message, turn_id):
            background.append((message, turn_id))
            delivered.set()

        handle.background_message_callback = on_background
        handle._start_message_pump()
        try:
            handle.next_turn_id = "origin-turn"
            await handle.query("first")
            response_task = asyncio.create_task(_collect(handle.receive_response()))
            await query.queue.put({
                "type": "system", "subtype": "task_started",
                "task_id": "task-1", "description": "Background review",
                "uuid": "u1", "session_id": "session-1",
                "tool_use_id": "agent-tool", "task_type": "agent",
            })
            await query.queue.put(result())
            await query.queue.put({
                "type": "system", "subtype": "task_progress",
                "task_id": "task-1", "description": "Background review",
                "usage": {"total_tokens": 10}, "uuid": "u2",
                "session_id": "session-1", "tool_use_id": "agent-tool",
                "last_tool_name": "Read",
            })
            first = await asyncio.wait_for(response_task, timeout=1)
            await asyncio.wait_for(query.progress_read.wait(), timeout=1)
            await asyncio.sleep(0)
            assert isinstance(first[0], TaskStartedMessage)
            assert isinstance(first[-1], ResultMessage)
            assert background == []  # Result has not been released by Machine.

            handle.release_background_messages()
            await asyncio.wait_for(delivered.wait(), timeout=1)
            assert isinstance(background[0][0], TaskProgressMessage)
            assert background[0][1] == "origin-turn"

            # A delayed old-task event racing the next query is consumed by the
            # same pump and delivered through that turn's response queue. The
            # shared translator maps still attach it to its original turn.
            await handle.query("second")
            second_task = asyncio.create_task(_collect(handle.receive_response()))
            await query.queue.put({
                "type": "system", "subtype": "task_notification",
                "task_id": "task-1", "status": "completed",
                "output_file": "/private/output", "summary": "done",
                "uuid": "u3", "session_id": "session-1",
                "tool_use_id": "agent-tool",
            })
            await query.queue.put(result())
            second = await asyncio.wait_for(second_task, timeout=1)
            assert isinstance(second[0], TaskNotificationMessage)
            assert isinstance(second[-1], ResultMessage)
            assert len(background) == 1
            assert query.consumers == 1
        finally:
            handle.release_background_messages()
            await handle._stop_message_pump()

    async def _collect(source):
        return [message async for message in source]

    asyncio.run(run())


def test_sdk_pump_releases_turn_barrier_on_query_failure_and_disconnect():
    async def run():
        class Query:
            def __init__(self):
                self.queue = asyncio.Queue()

            async def receive_messages(self):
                while True:
                    yield await self.queue.get()

        class Client:
            def __init__(self):
                self._query = Query()
                self.fail = True

            async def query(self, _prompt):
                if self.fail:
                    raise RuntimeError("query failed")

            async def disconnect(self):
                return None

        handle = SdkHandle(SimpleNamespace(turn_reader_queue_cap=2))
        client = Client()
        handle.client = client
        handle._start_message_pump()
        try:
            try:
                await handle.query("fails")
            except RuntimeError as exc:
                assert str(exc) == "query failed"
            else:
                raise AssertionError("query failure was not propagated")
            failed_barrier = handle._turn_background_release
            assert failed_barrier is not None and failed_barrier.is_set()

            client.fail = False
            await handle.query("disconnects")
            disconnect_barrier = handle._turn_background_release
            assert disconnect_barrier is not None
            assert disconnect_barrier.is_set() is False
            await handle.disconnect()
            assert disconnect_barrier.is_set() is True
            assert handle.client is None
        finally:
            if handle._message_pump_task is not None:
                await handle._stop_message_pump()

    asyncio.run(run())
