"""Source clocks, visible segment fences and public native commands."""
import json
from datetime import datetime

import pytest

from cc_remote.wrapper.codex_stream import (
    codex_next_user_boundary_ts, codex_translate_history,
)
from cc_remote.wrapper.history_store import materialize_history_turns


def row(second, payload, kind="event_msg"):
    return {"timestamp": f"2026-09-27T17:00:{second:02d}Z", "type": kind, "payload": payload}


def item(second, value):
    return row(second, {"type": "item_completed", "turn_id": "native", "item": value})


def user(second, uid):
    return item(second, {"type": "UserMessage", "id": uid,
                         "content": [{"type": "text", "text": uid}]})


def write(path, rows):
    offsets = []
    with path.open("wb") as stream:
        for value in rows:
            offsets.append(stream.tell())
            stream.write((json.dumps(value) + "\n").encode())
    return offsets


def stamp(second):
    return datetime.fromisoformat(row(second, {})["timestamp"]).timestamp()


def test_history_events_retain_source_timestamps_including_delayed_agent_rows(tmp_path):
    path = tmp_path / "source.jsonl"
    write(path, [
        row(0, {"type": "task_started", "turn_id": "native"}), user(1, "first"),
        row(2, {"type": "agent_message", "message": "progress", "phase": "commentary"}),
        row(8, {"type": "function_call", "call_id": "tool", "name": "exec_command",
                "arguments": '{"command":"ls"}'}, "response_item"),
        row(9, {"type": "function_call_output", "call_id": "tool", "output": "ok"}, "response_item"),
        row(10, {"type": "context_compacted", "id": "compact"}),
        item(11, {"type": "AgentMessage", "id": "answer", "phase": "final_answer",
                  "content": [{"type": "Text", "text": "done"}]}),
        row(12, {"type": "task_complete", "turn_id": "native"}),
    ])
    events, _ = codex_translate_history(str(path), 1024)
    assert [e.msg_id for e in events if e.type == "user_msg"] == ["first"]
    assert all(stamp(0) <= e.ts <= stamp(12) for e in events)
    assert next(e for e in events if e.type == "delta" and e.text == "progress").ts == stamp(2)
    assert next(e for e in events if e.type == "tool_use").ts == stamp(8)
    assert next(e for e in events if e.type == "tool_result").ts == stamp(9)
    assert [e.model_dump() for e in events] == [
        e.model_dump() for e in codex_translate_history(str(path), 1024)[0]]
    turn, = materialize_history_turns([e.model_dump() for e in events])
    assert turn["processStartedTs"] == int(stamp(2) * 1000)
    assert turn["processDoneTs"] <= turn["doneTs"] == int(stamp(12) * 1000)


@pytest.mark.parametrize("legacy_pair", [False, True])
def test_bounded_steer_segment_closes_without_claiming_native_terminal(tmp_path, legacy_pair):
    path = tmp_path / "source.jsonl"
    rows = [row(0, {"type": "task_started", "turn_id": "native"}), user(1, "first"),
            row(2, {"type": "agent_message", "message": "progress", "phase": "commentary"})]
    if legacy_pair:
        rows += [row(9, {"type": "message", "role": "user", "id": "next"}, "response_item"),
                 row(10, {"type": "user_message", "message": "next"})]
    else:
        rows += [user(10, "next")]
    offsets = write(path, rows)
    end_ts = codex_next_user_boundary_ts(str(path), offsets[3], "native")
    assert end_ts == stamp(10) - 0.001
    assert codex_next_user_boundary_ts(str(path), offsets[3], "native",
                                      end_offset=path.stat().st_size - 1) is None
    events, _ = codex_translate_history(str(path), 1024, end_offset=offsets[3],
                                       snapshot_in_progress=True, segment_end_ts=end_ts)
    terminal = events[-1]
    assert terminal.type == "turn_end" and terminal.result.subtype == "steered"
    assert not terminal.result.is_error and terminal.turn_id is None
    assert terminal.ts == end_ts
    assert any(e.type == "delta" and e.text == "progress" for e in events)
    # A byte window inside the response and a frozen active EOF are not fences.
    assert codex_next_user_boundary_ts(str(path), offsets[2], "native") is None
    active, _ = codex_translate_history(str(path), 1024, end_offset=offsets[3], snapshot_in_progress=True)
    assert not any(e.type == "turn_end" for e in active)


@pytest.mark.parametrize("camel", [False, True])
def test_native_commands_are_readable_bounded_and_deduplicated(tmp_path, camel):
    path = tmp_path / "commands.jsonl"
    command = {"type": "CommandExecution", "id": "exec-1", "command": ["rg", "a b", "."],
               "cwd": "/project", "status": "Completed", "exit_code": 1,
               "aggregated_output": "native output" * 100, "process_id": "pid-1",
               "parsed_cmd": [{"type": "Search", "query": "a b"}],
               "duration": {"secs": 2, "nanos": 500_000_000}}
    if camel:
        for snake, alternate in (("exit_code", "exitCode"), ("aggregated_output", "aggregatedOutput"),
                                 ("process_id", "processId"), ("parsed_cmd", "commandActions")):
            command[alternate] = command.pop(snake)
        command["durationMs"] = 2500
        del command["duration"]
    write(path, [row(0, {"type": "task_started", "turn_id": "native"}), user(1, "first"),
                 row(2, {"type": "function_call", "call_id": "exec-1", "name": "exec_command",
                         "arguments": '{}'}, "response_item"),
                 item(4, command), item(4, command),
                 row(5, {"type": "task_complete", "turn_id": "native"})])
    events, _ = codex_translate_history(str(path), 64)
    use, = [e for e in events if e.type == "tool_use"]
    result, = [e for e in events if e.type == "tool_result"]
    assert use.tool_use_id == result.tool_use_id == "exec-1"
    assert use.title == "搜索 a b" and use.input["command"] == "rg 'a b' ."
    assert use.input["cwd"] == "/project" and use.input["process_id"] == "pid-1"
    assert use.ts == stamp(2) and result.ts == stamp(4)
    assert result.exit_code == 1 and result.status == "failed" and result.is_error
    assert result.duration_ms == 2500 and result.truncated


def test_unknown_source_time_never_becomes_reconstruction_time(tmp_path):
    path = tmp_path / "unknown-time.jsonl"
    write(path, [{"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}},
                 {"type": "event_msg", "payload": {"type": "agent_message", "message": "hello"}}])
    assert all(e.ts == 0 for e in codex_translate_history(str(path), 1024)[0])
