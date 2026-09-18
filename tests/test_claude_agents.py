"""Zero-token coverage for the read-only Claude Agent projection."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from claude_agent_sdk.types import (
    AssistantMessage,
    StreamEvent,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from cc_remote.wrapper import claude_agents as agent_module
from cc_remote.wrapper.claude_agents import (
    _MAX_AGENT_SOURCE_BYTES,
    _MAX_AGENT_TOOL_OWNERS,
    AgentSourceTooLarge,
    ClaudeAgentRegistry,
    SourceAgentLocation,
    resolve_source_agent,
    translate_source_agent,
)
from cc_remote.wrapper.stream import public_agent_run_id
from tests.test_multisession import _mk_ctx, _mk_machine


def _assistant(content, *, parent=None):
    return AssistantMessage(
        content=content,
        model="claude-test",
        parent_tool_use_id=parent,
    )


def test_agent_partial_output_is_live_coalesced_and_not_duplicated(monkeypatch):
    registry = ClaudeAgentRegistry(64 * 1024)
    tick = [10.0]
    monkeypatch.setattr(agent_module.time, "monotonic", lambda: tick[0])

    def delta(text):
        return registry.route(StreamEvent(
            uuid="child-message", session_id="session",
            parent_tool_use_id="root-agent", event={
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ))

    first = delta("First ")
    assert any(event.get("text") == "First " for event in first.events)
    tick[0] += 0.01
    assert delta("second ").events == ()
    tick[0] += 0.01
    assert delta("third ").events == ()
    tick[0] += 0.2
    spaced = delta("fourth ")
    assert [event["text"] for event in spaced.events
            if event["type"] == "delta"] == ["second third fourth "]
    tick[0] += 0.01
    assert delta("last").events == ()
    assembled = registry.route(_assistant(
        [TextBlock(text="First second third fourth last")], parent="root-agent"))
    assert [event["text"] for event in assembled.events
            if event["type"] == "delta"] == ["last"]
    run = registry.snapshot(public_agent_run_id("root-agent"))
    assert "".join(event["text"] for event in run.events
                   if event["type"] == "delta") == "First second third fourth last"
    assert run.pending_stream_events == []


def test_registry_separates_root_and_nested_agent_messages():
    registry = ClaudeAgentRegistry(64 * 1024)
    root = registry.route(_assistant([ToolUseBlock(
        id="root-agent",
        name="Agent",
        input={
            "description": "审查后端",
            "subagent_type": "reviewer",
            "prompt": "PRIVATE ROOT PROMPT",
        },
    )]))
    assert root.target == "main"
    root_run_id = public_agent_run_id("root-agent")
    assert registry.snapshot(root_run_id).title == "审查后端"

    completed = registry.route(UserMessage(
        content=[ToolResultBlock(
            tool_use_id="root-agent", content="done", is_error=False)],
        tool_use_result={"agentId": "private-agent-id", "status": "completed"},
    ))
    assert registry.snapshot(root_run_id).status == "succeeded"
    assert completed.touched_run_ids == (root_run_id,)

    detail = registry.route(_assistant([
        TextBlock(text="正在检查"),
        ToolUseBlock(
            id="nested-agent",
            name="Agent",
            input={
                "description": "检查测试",
                "subagent_type": "tester",
                "prompt": "PRIVATE NESTED PROMPT",
            },
        ),
    ], parent="root-agent"))
    assert detail.target == "detail"
    assert detail.run_id == root_run_id
    wire = json.dumps(detail.events, ensure_ascii=False)
    assert "正在检查" in wire
    assert "PRIVATE ROOT PROMPT" not in wire
    assert "PRIVATE NESTED PROMPT" not in wire

    nested_run = registry.snapshot(public_agent_run_id("nested-agent"))
    assert nested_run is not None
    assert nested_run.parent_run_id == root_run_id

    nested_completed = registry.route(UserMessage(
        content=[ToolResultBlock(
            tool_use_id="nested-agent", content="done", is_error=False)],
        tool_use_result={"agentId": "private-nested-id", "status": "completed"},
        parent_tool_use_id="root-agent",
    ))
    assert nested_run.status == "succeeded"
    assert nested_run.run_id in nested_completed.touched_run_ids


def test_parallel_agent_lifecycle_statuses_remain_isolated():
    registry = ClaudeAgentRegistry(64 * 1024)
    registry.route(_assistant([
        ToolUseBlock(id="agent-a", name="Agent", input={"description": "A"}),
        ToolUseBlock(id="agent-b", name="Agent", input={"description": "B"}),
    ]))
    registry.route(TaskStartedMessage(
        subtype="task_started", data={}, task_id="task-a",
        description="A", uuid="a", session_id="session",
        tool_use_id="agent-a", task_type="agent",
    ))
    registry.route(TaskStartedMessage(
        subtype="task_started", data={}, task_id="task-b",
        description="B", uuid="b", session_id="session",
        tool_use_id="agent-b", task_type="agent",
    ))
    update = registry.route(TaskUpdatedMessage(
        subtype="task_updated", data={}, task_id="task-a",
        patch={}, status="completed", session_id="session", uuid="ua",
    ))

    assert public_agent_run_id("agent-a") in update.touched_run_ids
    assert registry.snapshot(public_agent_run_id("agent-a")).status == "succeeded"
    assert registry.snapshot(public_agent_run_id("agent-b")).status == "running"


def test_local_background_bash_never_creates_clickable_agent_run():
    registry = ClaudeAgentRegistry(64 * 1024)
    registry.route(_assistant([ToolUseBlock(
        id="background-bash",
        name="Bash",
        input={"command": "make check", "run_in_background": True},
    )]))

    routed = registry.route(TaskStartedMessage(
        subtype="task_started", data={}, task_id="bash-task",
        description="Run checks", uuid="bash-start", session_id="session",
        tool_use_id="background-bash", task_type="local_bash",
    ))

    assert routed.target == "main"
    assert routed.touched_run_ids == ()
    assert registry.runs == {}

    explicit_agent = registry.route(TaskStartedMessage(
        subtype="task_started", data={}, task_id="agent-task",
        description="Review", uuid="agent-start", session_id="session",
        tool_use_id="unseen-agent-tool", task_type="local_agent",
    ))
    assert explicit_agent.touched_run_ids == (
        public_agent_run_id("unseen-agent-tool"),)


def test_agent_detail_fails_closed_for_missing_codex_and_work():
    async def run():
        machine, _transport = _mk_machine()
        command = SimpleNamespace(
            session_id="session", run_id="agent-run", request_id="request",
            client_id="browser", revision=None, detail_revision=None,
            before=None, limit=192,
        )

        codex = _mk_ctx("session", "session")
        codex.engine = "codex"
        machine.sessions["session"] = codex
        rejected_codex = await machine._handle_get_agent_detail(command)
        assert rejected_codex.authoritative is False
        assert "尚未生成" in rejected_codex.error

        work = _mk_ctx("session", "session")
        work.engine = "claude"
        work.space = "work"
        machine.sessions["session"] = work
        rejected_work = await machine._handle_get_agent_detail(command)
        assert rejected_work.authoritative is False
        assert "Work" in rejected_work.error

    asyncio.run(run())


def test_agent_tool_owner_index_is_strictly_bounded():
    registry = ClaudeAgentRegistry(64 * 1024)
    run = registry._ensure_run("root-agent")
    for index in range(_MAX_AGENT_TOOL_OWNERS + 17):
        registry._remember_tool_owner(f"tool-{index}", run)
    assert len(registry.tool_owner) == _MAX_AGENT_TOOL_OWNERS
    assert len(run.owned_tool_ids) == _MAX_AGENT_TOOL_OWNERS
    assert "tool-0" not in registry.tool_owner


def test_cold_agent_resolution_reads_sidecars_not_transcript_payloads(
    monkeypatch, tmp_path,
):
    main = tmp_path / "session.jsonl"
    main.write_text("")
    root = tmp_path / "session" / "subagents"
    root.mkdir(parents=True)
    transcript = root / "agent-private-a.jsonl"
    transcript.write_text('{"private":"payload"}\n')
    (root / "agent-private-a.meta.json").write_text(json.dumps({
        "toolUseId": "root-agent",
        "description": "检查边界",
        "spawnDepth": 1,
    }))
    monkeypatch.setattr(agent_module, "transcript_path", lambda _sid: str(main))
    monkeypatch.setattr(
        agent_module,
        "get_subagent_messages",
        lambda *_args, **_kwargs: pytest.fail(
            "identity lookup must not read an Agent transcript"),
    )
    monkeypatch.setattr(
        agent_module, "translate_subagent_history", lambda *_args: [])

    location = resolve_source_agent(
        "session", public_agent_run_id("root-agent"), str(tmp_path))

    assert location is not None
    assert location.agent_id == "private-a"
    assert location.title == "检查边界"
    assert location.source_path == str(transcript)


def test_cold_agent_translation_rejects_unbounded_sdk_read(
    monkeypatch, tmp_path,
):
    transcript = tmp_path / "agent-private.jsonl"
    with transcript.open("wb") as target:
        target.truncate(_MAX_AGENT_SOURCE_BYTES + 1)
    location = SourceAgentLocation(
        run_id=public_agent_run_id("root-agent"),
        title="大代理",
        parent_run_id=None,
        status="succeeded",
        agent_id="private",
        source_path=str(transcript),
    )
    monkeypatch.setattr(
        agent_module,
        "get_subagent_messages",
        lambda *_args, **_kwargs: pytest.fail(
            "oversized transcript must fail before the SDK whole-file read"),
    )

    with pytest.raises(AgentSourceTooLarge):
        translate_source_agent("session", location, str(tmp_path), 64 * 1024)


def test_agent_detail_distinguishes_nested_agent_from_background_bash(
    monkeypatch,
):
    location = SourceAgentLocation(
        run_id=public_agent_run_id("root-agent"),
        title="根代理",
        parent_run_id=None,
        status="succeeded",
        agent_id="private-root",
        source_path=None,
    )
    background_notification = """<task-notification>
<task-id>bash-task</task-id>
<tool-use-id>background-bash</tool-use-id>
<status>completed</status>
<summary>checks passed</summary>
</task-notification>"""
    nested_notification = """<task-notification>
<task-id>nested-task</task-id>
<tool-use-id>nested-agent</tool-use-id>
<status>completed</status>
<summary>review passed</summary>
</task-notification>"""
    messages = [
        SimpleNamespace(
            uuid="tools", type="assistant", message={
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "background-bash",
                     "name": "Bash", "input": {
                         "command": "make check", "run_in_background": True,
                     }},
                    {"type": "tool_use", "id": "nested-agent",
                     "name": "Agent", "input": {
                         "description": "Review", "subagent_type": "reviewer",
                         "prompt": "PRIVATE NESTED PROMPT",
                     }},
                ],
            },
        ),
        SimpleNamespace(
            uuid="bash-notification", type="user",
            message={"role": "user", "content": background_notification},
        ),
        SimpleNamespace(
            uuid="agent-notification", type="user",
            message={"role": "user", "content": nested_notification},
        ),
    ]
    monkeypatch.setattr(
        agent_module, "get_subagent_messages",
        lambda *_args, **_kwargs: messages,
    )

    detail = translate_source_agent(
        "session", location, "/tmp/project", 64 * 1024)
    processes = {
        event["item_id"]: event
        for event in detail.events if event["type"] == "process"
    }

    assert processes["bash-task"]["kind"] == "task"
    assert processes["bash-task"]["parent_id"] == "background-bash"
    nested_run_id = public_agent_run_id("nested-agent")
    assert processes[nested_run_id]["kind"] == "agent"
    assert processes[nested_run_id]["parent_id"] == "nested-agent"
    assert "PRIVATE NESTED PROMPT" not in json.dumps(
        detail.events, ensure_ascii=False)
