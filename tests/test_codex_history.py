"""Zero-token tests for the official Codex persisted-history projection."""
from __future__ import annotations

import asyncio
import json

import pytest

import cc_remote.wrapper.codex_stream as codex_stream_module
from cc_remote.wrapper.codex_history import (
    CodexHistoryCursorError,
    CodexHistoryInvalidResponse,
    CodexHistoryUnsupported,
    CodexOfficialHistory,
)
from cc_remote.wrapper.codex_rpc import (
    CodexRpcRejected,
    CodexRpcResponseTooLarge,
)
from cc_remote.wrapper.codex_stream import (
    codex_history_boundary_process_start,
    codex_history_native_witness,
    codex_history_process_append,
    codex_history_process_witnesses,
    codex_history_image_views,
    codex_rollout_task_bindings,
    codex_history_turn_user,
    codex_history_turn_users,
    codex_history_window,
    codex_history_window_info,
    codex_translate_history,
)
from cc_remote.protocol import UserMsg


_PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)


def _user(
    item_id: str,
    text: str,
    *,
    client_id: str | None = None,
) -> dict:
    return {
        "type": "userMessage",
        "id": item_id,
        "clientId": client_id,
        "content": [{"type": "text", "text": text}],
    }


def _agent(item_id: str, text: str, *, phase: str = "final") -> dict:
    return {
        "type": "agentMessage",
        "id": item_id,
        "text": text,
        "phase": phase,
    }


def _turn(
    turn_id: str,
    items: list[dict],
    *,
    status: str = "completed",
    items_view: str = "summary",
    started_at: int | None = 100,
    completed_at: int | None = 102,
    duration_ms: int | None = 2000,
    error: dict | None = None,
) -> dict:
    return {
        "id": turn_id,
        "status": status,
        "itemsView": items_view,
        "items": items,
        "startedAt": started_at,
        "completedAt": completed_at,
        "durationMs": duration_ms,
        "error": (
            error or {"message": "provider detail"}
            if status == "failed"
            else None
        ),
    }


def test_rollout_task_binding_requires_exact_native_user_item(tmp_path):
    path = tmp_path / "rollout.jsonl"
    rows = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "id": "unrelated-user-item",
                "content": [{"type": "input_text", "text": "same prompt"}],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": "wrong-task",
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "same prompt",
                "turn_id": "text-only-task",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "id": "official-user-item",
                "content": [{"type": "input_text", "text": "same prompt"}],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": "rollout-task",
                },
            },
        },
    ]
    path.write_bytes(b"".join(
        json.dumps(row).encode() + b"\n" for row in rows
    ))

    assert codex_rollout_task_bindings(
        str(path), {"official-user-item", "missing-user-item"},
    ) == {"official-user-item": "rollout-task"}
    assert codex_rollout_task_bindings(
        str(path), {"missing-user-item"},
    ) == {}


def test_rollout_task_binding_is_bounded_to_recent_source_tail(tmp_path):
    path = tmp_path / "rollout.jsonl"
    witness = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "id": "official-user-item",
            "internal_chat_message_metadata_passthrough": {
                "turn_id": "old-rollout-task",
            },
        },
    }
    path.write_bytes(
        json.dumps(witness).encode() + b"\n" + b'{}\n' * 1024
    )

    assert codex_rollout_task_bindings(
        str(path), {"official-user-item"}, max_scan_bytes=1024,
    ) == {}


def test_rollout_image_view_supplement_is_turn_bound_and_binary_free(
    monkeypatch, tmp_path,
):
    path = tmp_path / "rollout.jsonl"
    rows = [
        {
            "timestamp": "2026-07-30T06:40:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-1"},
        },
        {
            "timestamp": "2026-07-30T06:40:01Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "turn_id": "native-1",
                "message": "inspect",
            },
        },
        {
            "timestamp": "2026-07-30T06:40:02Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "id": "fc-image-1",
                "name": "view_image",
                "arguments": json.dumps({
                    "path": "/tmp/chart.png",
                    "detail": "original",
                }),
                "call_id": "call-image-1",
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": "native-1",
                },
            },
        },
        {
            "timestamp": "2026-07-30T06:40:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-image-1",
                "output": [{
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{_PNG_1X1}",
                    "detail": "original",
                }],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": "native-1",
                },
            },
        },
        {
            "timestamp": "2026-07-30T06:40:04Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "turn_id": "native-1",
                "message": "now inspect the second image",
            },
        },
        {
            "timestamp": "2026-07-30T06:40:05Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "id": "fc-image-steer",
                "name": "view_image",
                "arguments": '{"path":"/tmp/steered.png"}',
                "call_id": "call-image-steer",
            },
        },
        {
            "timestamp": "2026-07-30T06:40:06Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-image-steer",
                "output": [{
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{_PNG_1X1}",
                }],
            },
        },
        {
            "timestamp": "2026-07-30T06:41:00Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "native-1"},
        },
        {
            "timestamp": "2026-07-30T06:42:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-2"},
        },
        {
            "timestamp": "2026-07-30T06:42:01Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "id": "fc-image-2",
                "name": "view_image",
                "arguments": '{"path":"/tmp/other.png"}',
                "call_id": "call-image-2",
            },
        },
    ]
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        codex_stream_module,
        "_reverse_jsonl_records",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("exact turn lookup must not walk every JSONL row")
        ),
    )

    views = codex_history_image_views(
        str(path), "native-1", segment_index=0)

    assert len(views) == 1
    view = views[0]
    assert view.call_id == "call-image-1"
    assert view.event.item_id == "fc-image-1"
    assert view.event.tool == "view_image"
    assert view.event.input is not None
    assert view.event.input["file_path"] == "/tmp/chart.png"
    assert view.event.input["history_image"]["image_id"].startswith("img-")
    assert view.media_type == "image/png"
    assert view.width == 1 and view.height == 1
    assert view.data is not None
    wire = view.event.model_dump_json()
    assert _PNG_1X1 not in wire
    assert "steered.png" not in wire
    assert "other.png" not in wire
    steered = codex_history_image_views(
        str(path), "native-1", segment_index=1)
    assert len(steered) == 1
    assert steered[0].event.input is not None
    assert steered[0].event.input["file_path"] == "/tmp/steered.png"
    assert "chart.png" not in steered[0].event.model_dump_json()
    assert codex_history_image_views(
        str(path), "native-1", segment_index=2) == ()
    translated, _model = codex_translate_history(str(path), 64 * 1024)
    translated_wire = "\n".join(
        event.model_dump_json() for event in translated)
    assert _PNG_1X1 not in translated_wire
    assert "图片已读取" in translated_wire


def test_summary_page_is_chronological_and_preserves_native_identity():
    calls: list[tuple[str, dict]] = []

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        assert cwd is None
        return {
            "data": [
                _turn(
                    "native-new",
                    [_user("user-new", "new", client_id="client-new"),
                     _agent("answer-new", "new answer")],
                    status="interrupted",
                    started_at=200,
                    completed_at=203,
                    duration_ms=3000,
                ),
                _turn(
                    "native-old",
                    [_user("user-old", "old", client_id="client-old"),
                     _agent("answer-old", "old answer")],
                ),
            ],
            "nextCursor": "older-page",
            "backwardsCursor": "newer-page",
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-1", before=None, limit=2)

        assert calls == [("thread/turns/list", {
            "threadId": "thread-1",
            "cursor": None,
            "limit": 2,
            "sortDirection": "desc",
            "itemsView": "summary",
        })]
        assert [turn["id"] for turn in page.turns] == [
            "user-old", "user-new"]
        assert [turn["clientMsgId"] for turn in page.turns] == [
            "client-old", "client-new"]
        assert page.turns[0]["forkPointId"] == "native-old"
        assert page.turns[0]["durationMs"] == 2000
        assert page.turns[0]["ts"] == 100_000
        assert page.turns[0]["doneTs"] == 102_000
        assert page.turns[1]["interrupted"] is True
        assert page.oldest_id == "user-old"
        assert page.newest_id == "user-new"
        assert page.has_more is True
        assert page.native_turn_ids == ("native-new", "native-old")
        assert page.turns[0]["blocks"][-1]["text"] == "old answer"
        assert page.turns[0]["detailEventCount"] == 0
        assert page.turns[0]["processDetailState"] == "unknown"
        assert page.turns[0]["detailReasons"] == []

    asyncio.run(run())


def test_native_history_witness_ignores_abnormal_payloads_and_dedupes_steers(
    tmp_path,
):
    path = tmp_path / "projection-witness.jsonl"
    rows = [
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-old"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "turn_id": "native-old",
                "message": "old prompt",
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "native-old"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-new"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "turn_id": "native-new",
                "message": "first prompt",
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {"credits": {"balance": "not-a-float"}},
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "turn_id": "native-new",
                "message": "steered prompt",
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "native-new"},
        },
    ]
    with path.open("wb") as target:
        for row in rows[:5]:
            target.write(json.dumps(row).encode() + b"\n")
        target.write(b'{"oversized":"' + b"x" * (1024 * 1024 + 1) + b'"}\n')
        target.write(b'{not-json}\n')
        for row in rows[5:]:
            target.write(json.dumps(row).encode() + b"\n")

    witness = codex_history_native_witness(
        str(path), max_turns=4, max_scan_bytes=4 * 1024 * 1024)

    assert witness.turn_ids == ("native-new", "native-old")
    assert witness.scanned_to_start is True
    assert witness.has_more_turns is False

    bounded = codex_history_native_witness(
        str(path), max_turns=1, max_scan_bytes=4 * 1024 * 1024)
    assert bounded.turn_ids == ("native-new",)
    assert bounded.scanned_to_start is True
    assert bounded.has_more_turns is True


def test_native_history_witness_recovers_public_process_without_direct_reply(
    tmp_path,
):
    path = tmp_path / "process-witness.jsonl"

    def row(timestamp, row_type, payload):
        return {
            "timestamp": timestamp,
            "type": row_type,
            "payload": payload,
        }

    rows = [
        row("2026-08-21T01:00:00Z", "event_msg", {
            "type": "task_started", "turn_id": "native-direct",
        }),
        row("2026-08-21T01:00:01Z", "response_item", {
            "type": "message", "role": "user", "id": "user-direct",
        }),
        row("2026-08-21T01:00:01Z", "event_msg", {
            "type": "user_message", "turn_id": "native-direct",
            "message": "hello",
        }),
        row("2026-08-21T01:00:02Z", "response_item", {
            "type": "reasoning", "id": "private-reasoning",
        }),
        row("2026-08-21T01:00:03Z", "event_msg", {
            "type": "agent_message", "phase": "final_answer",
            "message": "hi",
        }),
        row("2026-08-21T01:00:04Z", "event_msg", {
            "type": "task_complete", "turn_id": "native-direct",
        }),
        row("2026-08-21T01:01:00Z", "event_msg", {
            "type": "task_started", "turn_id": "native-process",
        }),
        row("2026-08-21T01:01:01Z", "response_item", {
            "type": "message", "role": "user", "id": "user-process",
        }),
        row("2026-08-21T01:01:01Z", "event_msg", {
            "type": "user_message", "turn_id": "native-process",
            "message": "inspect",
        }),
        row("2026-08-21T01:01:05Z", "response_item", {
            "type": "custom_tool_call", "id": "call-1",
            "call_id": "call-1", "name": "exec_command", "input": {},
        }),
        row("2026-08-21T01:01:08Z", "response_item", {
            "type": "custom_tool_call_output", "call_id": "call-1",
            "output": "ok",
        }),
        row("2026-08-21T01:01:09Z", "event_msg", {
            "type": "agent_message", "phase": "final_answer",
            "message": "done",
        }),
        row("2026-08-21T01:01:10Z", "event_msg", {
            "type": "task_complete", "turn_id": "native-process",
        }),
    ]
    path.write_bytes(b"".join(
        json.dumps(value).encode() + b"\n" for value in rows
    ))

    witness = codex_history_native_witness(
        str(path), max_turns=4, max_scan_bytes=4 * 1024 * 1024)

    assert witness.turn_ids == ("native-process", "native-direct")
    assert "user-direct" not in witness.process_by_visible_id
    process = witness.process_by_visible_id["user-process"]
    assert process.started_ms == 1_787_274_065_000
    assert process.done_ms == 1_787_274_068_000


def test_boundary_process_start_does_not_borrow_newer_steer_work(tmp_path):
    path = tmp_path / "direct-then-steer.jsonl"
    rows = [
        {
            "timestamp": "2026-08-21T01:00:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-multi"},
        },
        {
            "timestamp": "2026-08-21T01:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "hello"},
        },
        {
            "timestamp": "2026-08-21T01:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "phase": "final_answer",
                "message": "hi",
            },
        },
        {
            "timestamp": "2026-08-21T01:00:03Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "inspect"},
        },
        {
            "timestamp": "2026-08-21T01:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "id": "call-steer",
                "call_id": "call-steer",
                "name": "exec_command",
                "input": {},
            },
        },
    ]
    path.write_bytes(b"".join(
        json.dumps(value).encode() + b"\n" for value in rows
    ))

    assert codex_history_boundary_process_start(str(path), 0) is None
    window = codex_history_window_info(
        str(path), before=None, limit=1,
    )
    assert window.newest_native_turn_id == "native-multi"
    assert window.newest_segment_index == 1


def test_forced_window_keeps_native_owner_separate_from_user_cursor(tmp_path):
    path = tmp_path / "native-owner-vs-user-item.jsonl"
    task = {
        "type": "event_msg",
        "payload": {"type": "task_started", "turn_id": "native-turn"},
    }
    user_item = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "id": "msg-native-user",
            "content": [{"type": "input_text", "text": "inspect"}],
        },
    }
    user = {
        "type": "event_msg",
        "payload": {"type": "user_message", "message": "inspect"},
    }
    compact = {
        "type": "compacted",
        "payload": {"replacement_history": ["x" * (1100 * 1024)]},
    }
    path.write_text("".join(
        json.dumps(row) + "\n" for row in (task, user_item, user, compact)
    ))

    window = codex_history_window_info(
        str(path), before=None, limit=1, max_bytes=1024 * 1024,
    )
    assert window.forced_oldest_cursor == "msg-native-user"
    assert window.forced_boundary_offset == 0
    assert window.forced_native_turn_id == "native-turn"
    assert window.forced_segment_index == 0


def test_native_process_witness_numbers_steer_segments_chronologically(
    tmp_path,
):
    path = tmp_path / "steer-process-witness.jsonl"

    def row(timestamp, row_type, payload):
        return {
            "timestamp": timestamp,
            "type": row_type,
            "payload": payload,
        }

    rows = [
        row("2026-08-21T01:10:00Z", "event_msg", {
            "type": "task_started", "turn_id": "native-multi",
        }),
        row("2026-08-21T01:10:01Z", "response_item", {
            "type": "message", "role": "user", "id": "rollout-first",
        }),
        row("2026-08-21T01:10:01Z", "event_msg", {
            "type": "user_message", "turn_id": "native-multi",
            "message": "first",
        }),
        row("2026-08-21T01:10:02Z", "response_item", {
            "type": "custom_tool_call", "id": "call-first",
            "call_id": "call-first", "name": "exec_command", "input": {},
        }),
        row("2026-08-21T01:10:03Z", "response_item", {
            "type": "custom_tool_call_output", "call_id": "call-first",
            "output": "first",
        }),
        row("2026-08-21T01:10:04Z", "response_item", {
            "type": "message", "role": "user", "id": "rollout-steer",
        }),
        row("2026-08-21T01:10:04Z", "event_msg", {
            "type": "user_message", "turn_id": "native-multi",
            "message": "continue",
        }),
        row("2026-08-21T01:10:05Z", "response_item", {
            "type": "custom_tool_call", "id": "call-steer",
            "call_id": "call-steer", "name": "exec_command", "input": {},
        }),
        row("2026-08-21T01:10:06Z", "response_item", {
            "type": "custom_tool_call_output", "call_id": "call-steer",
            "output": "steer",
        }),
        row("2026-08-21T01:10:07Z", "event_msg", {
            "type": "task_complete", "turn_id": "native-multi",
        }),
    ]
    path.write_bytes(b"".join(
        json.dumps(value).encode() + b"\n" for value in rows
    ))

    witness = codex_history_native_witness(
        str(path), max_turns=2, max_scan_bytes=4 * 1024 * 1024)

    assert set(witness.process_by_native_segment) == {
        ("native-multi", 0), ("native-multi", 1),
    }
    first = witness.process_by_native_segment[("native-multi", 0)]
    steer = witness.process_by_native_segment[("native-multi", 1)]
    assert (first.started_ms, first.done_ms) == (
        1_787_274_602_000, 1_787_274_603_000)
    assert (steer.started_ms, steer.done_ms) == (
        1_787_274_605_000, 1_787_274_606_000)
    assert witness.offset_by_native_segment[("native-multi", 0)] \
        < witness.offset_by_native_segment[("native-multi", 1)]


def test_older_history_process_witness_starts_after_browser_cursor(tmp_path):
    path = tmp_path / "older-process-witness.jsonl"
    rows = []
    for index in range(6):
        turn_id = f"native-{index}"
        rows.extend((
            {
                "timestamp": f"2026-08-21T02:{index:02d}:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": turn_id},
            },
            {
                "timestamp": f"2026-08-21T02:{index:02d}:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message", "turn_id": turn_id,
                    "message": f"prompt {index}",
                },
            },
            {
                "timestamp": f"2026-08-21T02:{index:02d}:02Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call", "id": f"call-{index}",
                    "call_id": f"call-{index}", "name": "exec_command",
                    "input": {},
                },
            },
            {
                "timestamp": f"2026-08-21T02:{index:02d}:03Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": turn_id},
            },
        ))
    path.write_bytes(b"".join(
        json.dumps(value).encode() + b"\n" for value in rows
    ))

    witnesses = codex_history_process_witnesses(
        str(path), before="native-4", max_turns=2,
    )

    assert set(witnesses.process_by_visible_id) == {"native-3", "native-2"}
    assert set(witnesses.offset_by_visible_id) == {"native-3", "native-2"}

    # Cold official pages can use synthetic item ids absent from the rollout.
    # No offset from the preceding (byte-bounded) head is required to recover
    # this exact requested page, and neighboring native tasks must not leak in.
    cold = codex_history_process_witnesses(
        str(path), before="item-opaque", max_turns=2,
        native_turn_ids=("native-3", "native-2"),
    )
    assert cold.process_by_native_segment == witnesses.process_by_native_segment
    assert cold.offset_by_native_segment == witnesses.offset_by_native_segment
    assert codex_history_process_witnesses(
        str(path), before="item-opaque", max_turns=1,
        native_turn_ids=("missing-native",),
    ).process_by_native_segment == {}

    next_page = codex_history_process_witnesses(
        str(path),
        before="native-2",
        before_offset=witnesses.offset_by_visible_id["native-2"],
        max_turns=2,
        native_turn_ids=("native-1", "native-0"),
    )
    assert set(next_page.process_by_visible_id) == {"native-1", "native-0"}


@pytest.mark.parametrize("payload", [
    {"type": "task_started", "turn_id": "next-native"},
    {"type": "user_message", "turn_id": "native", "message": "steer"},
    {"type": "item_completed", "item": {"type": "UserMessage", "content": []}},
    {"type": "message", "role": "user", "id": "paired-steer"},
    {"type": "thread_goal_updated", "objective": "next goal"},
])
def test_process_append_rejects_changed_user_or_task_ownership(tmp_path, payload):
    path = tmp_path / "process-append-boundary.jsonl"
    path.write_text(
        '{"type":"event_msg","payload":{"type":"task_started","turn_id":"native"}}\n'
        '{"type":"event_msg","payload":{"type":"user_message","message":"test"}}\n')
    previous = codex_history_process_witnesses(
        str(path), before="item", max_turns=1, native_turn_ids=("native",))
    assert previous.append_segment == ("native", 0)
    start = path.stat().st_size
    with path.open("a") as output:
        output.write(json.dumps({"type": "event_msg", "payload": payload}) + "\n")
    assert codex_history_process_append(
        str(path), previous=previous, start_offset=start,
        end_offset=path.stat().st_size,
    ) is None


@pytest.mark.parametrize("partial_prefix", [False, True])
def test_process_append_requires_complete_jsonl_seam(tmp_path, partial_prefix):
    path = tmp_path / "process-partial-append.jsonl"
    path.write_text(
        '{"type":"event_msg","payload":{"type":"task_started","turn_id":"native"}}\n'
        '{"type":"event_msg","payload":{"type":"user_message","message":"test"}}\n')
    previous = codex_history_process_witnesses(
        str(path), before="item", max_turns=1, native_turn_ids=("native",))
    if partial_prefix:
        with path.open("a") as output:
            output.write('{"type":"event_msg",')
    start = path.stat().st_size
    with path.open("a") as output:
        output.write('"payload":{"type":"exec_command_end"}}\n' if partial_prefix
                     else '{"type":"event_msg",')
    assert codex_history_process_append(
        str(path), previous=previous, start_offset=start,
        end_offset=path.stat().st_size,
    ) is None


def test_process_append_coordinates_require_the_frozen_native_head(tmp_path):
    path = tmp_path / "frozen-process-head.jsonl"
    old = (
        '{"type":"event_msg","payload":{"type":"task_started","turn_id":"old"}}\n'
        '{"type":"event_msg","payload":{"type":"user_message","message":"old"}}\n')
    path.write_text(old)
    frozen_end = path.stat().st_size
    with path.open("a") as output:
        output.write(
            '{"type":"event_msg","payload":{"type":"task_complete","turn_id":"old"}}\n'
            '{"type":"event_msg","payload":{"type":"task_started","turn_id":"new"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"new"}}\n')
    snapshot = codex_history_process_witnesses(
        str(path), before="item", max_turns=1,
        native_turn_ids=("old",), source_end_offset=frozen_end)
    assert snapshot.append_segment == ("old", 0)
    # A stale official page may lag behind the physical rollout head. Its old
    # native coordinates must never absorb the newer task's tool appends.
    current = codex_history_process_witnesses(
        str(path), before="item", max_turns=1, native_turn_ids=("old",))
    assert current.append_segment is None
    # Automatic no-user task continuation is intentionally left to the full
    # ownership parser, rather than inferring a native task from a visible row.
    path.write_text(old +
                    '{"type":"event_msg","payload":{"type":"task_started","turn_id":"auto"}}\n')
    continuation = codex_history_process_witnesses(
        str(path), before="item", max_turns=1, native_turn_ids=("old",))
    assert continuation.append_segment is None


def test_required_native_witness_extends_past_initial_tail_budget(tmp_path):
    path = tmp_path / "adaptive-process-witness.jsonl"
    rows = []
    expected = []
    for index in range(5):
        turn_id = f"native-{index}"
        expected.append(turn_id)
        rows.extend((
            {
                "timestamp": f"2026-08-21T03:{index:02d}:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": turn_id},
            },
            {
                "timestamp": f"2026-08-21T03:{index:02d}:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message", "turn_id": turn_id,
                    "message": f"prompt {index}",
                },
            },
            {
                "timestamp": f"2026-08-21T03:{index:02d}:02Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call", "id": f"call-{index}",
                    "call_id": f"call-{index}", "name": "exec_command",
                    "input": {},
                },
            },
            {
                "timestamp": f"2026-08-21T03:{index:02d}:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count", "padding": "x" * 400_000,
                },
            },
            {
                "timestamp": f"2026-08-21T03:{index:02d}:04Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": turn_id},
            },
        ))
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    bounded = codex_history_native_witness(
        str(path), max_turns=6, max_scan_bytes=1024 * 1024)
    assert len(bounded.turn_ids) < len(expected)

    adaptive = codex_history_native_witness(
        str(path),
        max_turns=6,
        max_scan_bytes=None,
        required_turn_ids=tuple(reversed(expected)),
    )
    assert adaptive.turn_ids == tuple(reversed(expected))
    assert set(adaptive.process_by_visible_id) == set(expected)
    assert set(adaptive.offset_by_visible_id) == set(expected)


def test_summary_page_recovers_goal_prompt_for_assistant_only_native_turn():
    recover_calls = []

    async def rpc(_method, _params, cwd=None):
        assert cwd is None
        return {
            "data": [_turn(
                "native-goal",
                [_agent("answer-goal", "proof")],
            )],
            "nextCursor": None,
        }

    async def recover_users(thread_id, native_turn_ids):
        recover_calls.append((thread_id, native_turn_ids))
        return {
            native_turn_id: UserMsg(
                msg_id=native_turn_id,
                prompt="证明泰勒展开",
            )
            for native_turn_id in native_turn_ids
        }

    async def run():
        page = await CodexOfficialHistory(
            64 * 1024,
            rpc=rpc,
            recover_users=recover_users,
        ).summary_page("thread-goal", before=None, limit=1)

        assert recover_calls == [(
            "thread-goal", ("native-goal",),
        )]
        assert [(turn["id"], turn["prompt"]) for turn in page.turns] == [
            ("native-goal", "证明泰勒展开"),
        ]

    asyncio.run(run())


def test_summary_page_retries_automatic_user_recovery_after_miss():
    recover_calls = []

    async def rpc(_method, _params, cwd=None):
        assert cwd is None
        return {
            "data": [_turn(
                "native-goal",
                [_agent("answer-goal", "proof")],
            )],
            "nextCursor": None,
        }

    async def recover_users(thread_id, native_turn_ids):
        recover_calls.append((thread_id, native_turn_ids))
        if len(recover_calls) == 1:
            return {}
        return {
            "native-goal": UserMsg(
                msg_id="native-goal",
                prompt="后来落盘的 Goal",
            ),
        }

    async def run():
        history = CodexOfficialHistory(
            64 * 1024,
            rpc=rpc,
            recover_users=recover_users,
        )
        first = await history.summary_page(
            "thread-goal", before=None, limit=1)
        second = await history.summary_page(
            "thread-goal", before=None, limit=1)
        third = await history.summary_page(
            "thread-goal", before=None, limit=1)

        assert first.turns[0]["prompt"] == ""
        assert second.turns[0]["prompt"] == "后来落盘的 Goal"
        assert third.turns[0]["prompt"] == "后来落盘的 Goal"
        assert recover_calls == [
            ("thread-goal", ("native-goal",)),
            ("thread-goal", ("native-goal",)),
        ]

    asyncio.run(run())


def test_summary_page_live_prompt_replaces_automatic_user_miss():
    recover_calls = []

    async def rpc(_method, _params, cwd=None):
        assert cwd is None
        return {
            "data": [_turn(
                "native-goal",
                [_agent("answer-goal", "done")],
            )],
            "nextCursor": None,
        }

    async def recover_users(thread_id, native_turn_ids):
        recover_calls.append((thread_id, native_turn_ids))
        return {}

    async def run():
        history = CodexOfficialHistory(
            64 * 1024,
            rpc=rpc,
            recover_users=recover_users,
        )
        first = await history.summary_page("thread-goal", before=None, limit=1)
        assert recover_calls == [(
            "thread-goal",
            ("native-goal",),
        )]
        assert first.turns[0]["prompt"] == ""

        history.remember_automatic_user(
            "thread-goal",
            "native-goal",
            UserMsg(msg_id="native-goal", prompt="实时回调 Goal"),
        )
        second = await history.summary_page("thread-goal", before=None, limit=1)
        assert len(recover_calls) == 1
        assert second.turns[0]["prompt"] == "实时回调 Goal"

    asyncio.run(run())


def test_failed_network_summary_keeps_specific_safe_product_copy():
    async def rpc(_method, _params, cwd=None):
        assert cwd is None
        return {
            "data": [_turn(
                "native-network",
                [_user("user-network", "continue"),
                 _agent("answer-network", "partial answer")],
                status="failed",
                error={
                    "message": "stream disconnected before completion: "
                               "error sending request for url "
                               "https://chatgpt.com/backend-api/codex/responses",
                    "codexErrorInfo": "other",
                },
            )],
            "nextCursor": None,
        }

    async def run():
        page = await CodexOfficialHistory(
            64 * 1024, rpc=rpc,
        ).summary_page("thread-network", before=None, limit=1)

        assert page.turns[0]["error"] == (
            "网络连接异常，请检查网络后重试。"
        )
        assert "chatgpt.com" not in page.turns[0]["error"]

    asyncio.run(run())


def test_summary_supports_assistant_only_and_multiple_steer_segments():
    async def rpc(_method, _params, cwd=None):
        assert cwd is None
        return {
            "data": [
                _turn(
                    "native-multi",
                    [
                        _user("user-first", "first"),
                        _agent("answer-first", "first answer"),
                        _user("user-steer", "steer", client_id="client-steer"),
                        _agent("answer-last", "last answer"),
                    ],
                ),
                _turn(
                    "native-auto",
                    [_agent("auto-answer", "continued automatically")],
                ),
            ],
            "nextCursor": None,
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-1", before=None, limit=2)

        assert [turn["id"] for turn in page.turns] == [
            "native-auto", "user-first", "user-steer"]
        auto, first, steer = page.turns
        assert auto["prompt"] == ""
        assert auto["forkPointId"] == "native-auto"
        assert first["done"] is True
        assert "forkPointId" not in first
        assert steer["forkPointId"] == "native-multi"
        assert steer["clientMsgId"] == "client-steer"
        assert page.native_segment_by_visible_id == {
            "native-auto": ("native-auto", 0),
            "user-first": ("native-multi", 0),
            "user-steer": ("native-multi", 1),
        }

    asyncio.run(run())


def test_rollout_detail_fallback_covers_every_visible_steer_segment():
    async def rpc(_method, _params, cwd=None):
        assert cwd is None
        return {
            "data": [
                _turn(
                    "native-newest",
                    [_user("user-newest", "newest"),
                     _agent("answer-newest", "done")],
                ),
                _turn(
                    "native-middle",
                    [_user("user-middle", "middle"),
                     _agent("answer-middle", "done")],
                ),
                _turn(
                    "native-steered",
                    [
                        _user("user-first", "first"),
                        _agent("answer-first", "first answer"),
                        _user("user-steer", "continue"),
                        _agent("answer-steer", "final answer"),
                    ],
                ),
                _turn(
                    "native-oldest",
                    [_user("user-oldest", "oldest"),
                     _agent("answer-oldest", "done")],
                ),
            ],
            "nextCursor": None,
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-visible-limit", before=None, limit=4)

        assert [turn["id"] for turn in page.turns] == [
            "user-oldest",
            "user-first",
            "user-steer",
            "user-middle",
            "user-newest",
        ]
        # Rollout pagination counts visible user segments, while the official
        # API limit counts native turns. The oldest visible row must therefore
        # retain a fallback window wide enough to include all five rows.
        fallback = history.rollout_fallback(
            "thread-visible-limit", "user-oldest")
        assert fallback.limit == 5
        assert fallback.native_turn_id == "native-oldest"

    asyncio.run(run())


def test_summary_applies_exact_durable_aliases_without_overriding_upstream():
    async def rpc(_method, _params, cwd=None):
        assert cwd is None
        return {
            "data": [_turn(
                "native-multi",
                [
                    _user("user-first", "first"),
                    _agent("answer-first", "first answer"),
                    _user(
                        "user-steer",
                        "steer",
                        client_id="upstream-steer",
                    ),
                    _agent("answer-last", "last answer"),
                ],
            )],
            "nextCursor": None,
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-1",
            before=None,
            limit=1,
            client_message_ids={
                "user-first": "browser-first",
                "user-steer": "stale-steer",
            },
            segment_client_message_ids={
                ("native-multi", 0): "segment-first",
                ("native-multi", 1): "segment-steer",
            },
        )

        first, steer = page.turns
        assert first["clientMsgId"] == "browser-first"
        assert steer["clientMsgId"] == "upstream-steer"

    asyncio.run(run())


def test_active_summary_hydrates_exact_full_turn_and_preserves_steers():
    calls = []
    summary = _turn(
        "native-active",
        [
            _user("user-first", "first"),
            _agent("answer-last", "latest only"),
        ],
        status="interrupted",
        completed_at=None,
        duration_ms=None,
    )
    full = _turn(
        "native-active",
        [
            _user("user-first", "first"),
            _agent("answer-first", "first answer", phase="commentary"),
            _user("user-steer", "continue", client_id="client-steer"),
            _agent("answer-after-steer", "still working", phase="commentary"),
        ],
        status="interrupted",
        items_view="full",
        completed_at=None,
        duration_ms=None,
    )

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        assert cwd is None
        if len(calls) == 1:
            return {"data": [summary], "nextCursor": None}
        assert params["itemsView"] == "full"
        return {"data": [full], "nextCursor": None}

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
            active_turn_ids={"native-active"},
        )

        assert [turn["id"] for turn in page.turns] == [
            "user-first", "user-steer"]
        assert page.turns[0]["done"] is True
        assert page.turns[1]["done"] is False
        assert page.turns[1]["clientMsgId"] == "client-steer"
        assert page.turns[1]["forkPointId"] == "native-active"
        assert [params["itemsView"] for _method, params in calls] == [
            "summary", "full"]

    asyncio.run(run())


def test_leading_context_compaction_belongs_to_following_user():
    summary = _turn(
        "native-compact",
        [
            _user("user-after-compact", "html有修改嘛？"),
            _agent("answer-after-compact", "done"),
        ],
    )
    full = _turn(
        "native-compact",
        [
            {"type": "contextCompaction", "id": "compact-leading"},
            _user("user-after-compact", "html有修改嘛？"),
            _agent("comment-after-compact", "checking", phase="commentary"),
            _agent("answer-after-compact", "done"),
        ],
        items_view="full",
    )

    async def rpc(_method, params, cwd=None):
        assert cwd is None
        turn = summary if params["itemsView"] == "summary" else full
        return {"data": [turn], "nextCursor": None}

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-compact",
            before=None,
            limit=1,
            hydrate_recent=1,
            include_live_detail=True,
        )

        assert len(page.turns) == 1
        turn = page.turns[0]
        assert turn["id"] == "user-after-compact"
        assert turn["prompt"] == "html有修改嘛？"
        assert [block["kind"] for block in turn["blocks"]] == [
            "process", "text", "text"]
        assert turn["blocks"][0]["item_id"] == "compact-leading"
        assert turn["blocks"][0]["processKind"] == "compaction"
        assert turn["done"] is True

    asyncio.run(run())


def test_non_compaction_prefix_remains_an_automatic_continuation():
    summary = _turn(
        "native-continuation",
        [
            _user("user-after-continuation", "next question"),
            _agent("next-answer", "next answer"),
        ],
    )
    full = _turn(
        "native-continuation",
        [
            _agent("automatic-answer", "background result"),
            _user("user-after-continuation", "next question"),
            _agent("next-answer", "next answer"),
        ],
        items_view="full",
    )

    async def rpc(_method, params, cwd=None):
        assert cwd is None
        turn = summary if params["itemsView"] == "summary" else full
        return {"data": [turn], "nextCursor": None}

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-continuation",
            before=None,
            limit=1,
            hydrate_recent=1,
            include_live_detail=True,
        )

        assert [turn["prompt"] for turn in page.turns] == [
            "", "next question"]
        assert page.turns[0]["id"] == "native-continuation"
        assert page.turns[1]["id"] == "user-after-continuation"

    asyncio.run(run())


def test_active_full_shape_survives_completed_summary_collapse():
    responses = [
        {
            "data": [_turn(
                "native-active",
                [_user("user-first", "first"), _agent("last", "latest")],
                status="interrupted",
                completed_at=None,
                duration_ms=None,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-active",
                [
                    _user("user-first", "first"),
                    _agent("answer-first", "first answer"),
                    _user("user-steer", "continue"),
                    _agent("answer-final", "final answer"),
                ],
                status="interrupted",
                items_view="full",
                completed_at=None,
                duration_ms=None,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-active",
                [_user("user-first", "first"),
                 _agent("answer-final", "final answer")],
                status="completed",
                completed_at=109,
                duration_ms=9000,
            )],
            "nextCursor": None,
        },
    ]

    async def rpc(_method, _params, cwd=None):
        assert cwd is None
        return responses.pop(0)

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        active = await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
            active_turn_ids={"native-active"},
        )
        completed = await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
        )

        assert [turn["id"] for turn in active.turns] == [
            "user-first", "user-steer"]
        assert [turn["id"] for turn in completed.turns] == [
            "user-first", "user-steer"]
        assert completed.turns[-1]["done"] is True
        assert completed.turns[-1]["durationMs"] == 9000

    asyncio.run(run())


def test_completed_summary_refreshes_stale_active_full_before_projection():
    calls = []
    responses = [
        {
            "data": [_turn(
                "native-active",
                [_user("user-first", "first"),
                 _agent("answer-working", "working", phase="commentary")],
                status="interrupted",
                completed_at=None,
                duration_ms=None,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-active",
                [
                    _user("user-first", "first"),
                    _agent(
                        "answer-working",
                        "working",
                        phase="commentary",
                    ),
                    _user("user-steer", "continue"),
                ],
                status="interrupted",
                items_view="full",
                completed_at=None,
                duration_ms=None,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-active",
                [_user("user-first", "first"),
                 _agent("answer-final", "final answer")],
                status="completed",
                completed_at=109,
                duration_ms=9000,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-active",
                [
                    _user("user-first", "first"),
                    _agent(
                        "answer-working",
                        "working",
                        phase="commentary",
                    ),
                    _user("user-steer", "continue"),
                    _agent("answer-final", "final answer"),
                ],
                status="completed",
                items_view="full",
                completed_at=109,
                duration_ms=9000,
            )],
            "nextCursor": None,
        },
    ]

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        assert cwd is None
        return responses.pop(0)

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
            active_turn_ids={"native-active"},
        )
        completed = await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
            hydrate_recent=1,
        )

        assert [params["itemsView"] for _method, params in calls] == [
            "summary", "full", "summary", "full"]
        assert [turn["id"] for turn in completed.turns] == [
            "user-first", "user-steer"]
        assert completed.turns[-1]["blocks"][-1]["text"] == "final answer"
        assert completed.turns[-1]["done"] is True
        assert responses == []

    asyncio.run(run())


def test_terminal_summary_keeps_final_when_full_refresh_is_oversized():
    calls = []
    responses = [
        {
            "data": [_turn(
                "native-active",
                [_user("user-first", "first")],
                status="interrupted",
                completed_at=None,
                duration_ms=None,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-active",
                [
                    _user("user-first", "first"),
                    _agent(
                        "answer-working",
                        "working",
                        phase="commentary",
                    ),
                    _user("user-steer", "continue"),
                ],
                status="interrupted",
                items_view="full",
                completed_at=None,
                duration_ms=None,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-active",
                [_user("user-first", "summary-only first"),
                 _agent("answer-final", "final answer")],
                status="completed",
                completed_at=109,
                duration_ms=9000,
            )],
            "nextCursor": None,
        },
        CodexRpcResponseTooLarge("terminal full turn is oversized"),
        {
            "data": [_turn(
                "native-active",
                [_user("user-first", "summary-only first"),
                 _agent("answer-final", "final answer")],
                status="completed",
                completed_at=109,
                duration_ms=9000,
            )],
            "nextCursor": None,
        },
    ]

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        assert cwd is None
        if responses:
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        raise CodexRpcResponseTooLarge("terminal full turn is oversized")

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
            active_turn_ids={"native-active"},
        )
        completed = await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
            hydrate_recent=1,
        )
        refreshed = await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
            hydrate_recent=1,
        )

        assert [params["itemsView"] for _method, params in calls] == [
            "summary", "full", "summary", "full", "summary"]
        assert [turn["id"] for turn in completed.turns] == [
            "user-first", "user-steer"]
        assert completed.turns[0]["prompt"] == "first"
        assert completed.turns[-1]["blocks"][-1]["text"] == "final answer"
        assert completed.turns[-1]["done"] is True
        assert refreshed.turns == completed.turns
        assert responses == []

    asyncio.run(run())


def test_terminal_summary_restores_user_after_leading_compaction_race():
    compact = {"type": "contextCompaction", "id": "compact-leading"}
    terminal_summary = _turn(
        "native-active",
        [
            _user("user-after-compact", "html有修改嘛？"),
            _agent("answer-final", "final answer"),
        ],
        status="completed",
        completed_at=109,
        duration_ms=9000,
    )
    responses = [
        {
            "data": [_turn(
                "native-active",
                [compact],
                status="interrupted",
                completed_at=None,
                duration_ms=None,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-active",
                [compact],
                status="interrupted",
                items_view="full",
                completed_at=None,
                duration_ms=None,
            )],
            "nextCursor": None,
        },
        {"data": [terminal_summary], "nextCursor": None},
        CodexRpcResponseTooLarge("terminal full turn is oversized"),
        {"data": [terminal_summary], "nextCursor": None},
    ]
    calls = []

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        assert cwd is None
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
            include_live_detail=True,
            active_turn_ids={"native-active"},
        )
        completed = await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
            hydrate_recent=1,
            include_live_detail=True,
        )
        refreshed = await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
            hydrate_recent=1,
            include_live_detail=True,
        )

        assert [params["itemsView"] for _method, params in calls] == [
            "summary", "full", "summary", "full", "summary"]
        assert len(completed.turns) == 1
        turn = completed.turns[0]
        assert turn["id"] == "user-after-compact"
        assert turn["prompt"] == "html有修改嘛？"
        assert turn["done"] is True
        assert [block["kind"] for block in turn["blocks"]] == [
            "process", "text"]
        assert turn["blocks"][0]["item_id"] == "compact-leading"
        assert turn["blocks"][1]["text"] == "final answer"
        assert refreshed.turns == completed.turns
        assert responses == []

    asyncio.run(run())


def test_newest_completed_turn_hydration_restores_cold_steer_segments():
    responses = [
        {
            "data": [_turn(
                "native-completed",
                [_user("user-first", "first"),
                 _agent("answer-final", "final answer")],
                status="completed",
                completed_at=109,
                duration_ms=9000,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-completed",
                [
                    _user("user-first", "first"),
                    _agent("answer-first", "first answer"),
                    _user("user-steer", "continue"),
                    _agent("answer-final", "final answer"),
                ],
                status="completed",
                items_view="full",
                completed_at=109,
                duration_ms=9000,
            )],
            "nextCursor": None,
        },
    ]

    async def rpc(_method, _params, cwd=None):
        assert cwd is None
        return responses.pop(0)

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-completed",
            before=None,
            limit=1,
            hydrate_recent=1,
        )

        assert [turn["id"] for turn in page.turns] == [
            "user-first", "user-steer"]
        assert [turn["prompt"] for turn in page.turns] == [
            "first", "continue"]
        assert page.turns[-1]["done"] is True
        assert page.turns[-1]["durationMs"] == 9000
        assert all(
            turn["processDetailState"] == "none"
            for turn in page.turns
        )
        assert all(turn["detailReasons"] == [] for turn in page.turns)

    asyncio.run(run())


def test_recent_full_process_does_not_invent_zero_second_interval():
    items = [
        _user("user-process", "inspect"),
        {
            "type": "commandExecution",
            "id": "command-process",
            "command": "pwd",
            "commandActions": [],
            "cwd": "/repo",
            "status": "completed",
            "aggregatedOutput": "/repo\n",
            "exitCode": 0,
            "durationMs": 12,
        },
        _agent("answer-process", "done"),
    ]
    responses = [
        {
            "data": [_turn("native-process", [items[0], items[-1]])],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-process", items, items_view="full",
            )],
            "nextCursor": None,
        },
    ]

    async def rpc(_method, _params, cwd=None):
        assert cwd is None
        return responses.pop(0)

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-process", before=None, limit=1, hydrate_recent=1,
        )

        turn = page.turns[0]
        assert turn["processDetailState"] == "present"
        assert "processStartedTs" not in turn
        assert "processDoneTs" not in turn

    asyncio.run(run())


def test_official_turn_without_timestamps_does_not_use_history_read_time():
    items = [
        _user("user-untimed", "inspect"),
        {
            "type": "commandExecution",
            "id": "command-untimed",
            "command": "pwd",
            "commandActions": [],
            "cwd": "/repo",
            "status": "completed",
            "aggregatedOutput": "/repo\n",
            "exitCode": 0,
            "durationMs": 12,
        },
        _agent("answer-untimed", "done"),
    ]

    responses = [{
        "data": [_turn(
            "native-untimed", [items[0], items[-1]],
            started_at=None, completed_at=None,
        )],
        "nextCursor": None,
    }, {
        "data": [_turn(
            "native-untimed", items, items_view="full",
            started_at=None, completed_at=None,
        )],
        "nextCursor": None,
    }]

    async def rpc(_method, _params, cwd=None):
        assert cwd is None
        return responses.pop(0)

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-untimed", before=None, limit=1, hydrate_recent=1,
        )

        assert all("ts" not in event for event in page.events)
        turn = page.turns[0]
        assert "ts" not in turn
        assert "doneTs" not in turn
        assert turn["processDetailState"] == "present"
        assert "processStartedTs" not in turn
        assert "processDoneTs" not in turn

    asyncio.run(run())


def test_recent_hydration_restores_just_finished_previous_turn():
    calls = []

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        if len(calls) == 1:
            return {
                "data": [
                    _turn(
                        "native-new",
                        [_user("user-new", "new"), _agent("answer-new", "new")],
                    ),
                    _turn(
                        "native-steered",
                        [_user("user-first", "first"),
                         _agent("answer-final", "final")],
                    ),
                ],
                "nextCursor": None,
            }
        assert params["itemsView"] == "full"
        assert params["limit"] == 2
        return {
            "data": [
                _turn(
                    "native-new",
                    [_user("user-new", "new"), _agent("answer-new", "new")],
                    items_view="full",
                ),
                _turn(
                    "native-steered",
                    [
                        _user("user-first", "first"),
                        _agent("answer-first", "first answer"),
                        _user("user-steer", "continue"),
                        _agent("answer-final", "final"),
                    ],
                    items_view="full",
                ),
            ],
            "nextCursor": None,
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-recent",
            before=None,
            limit=2,
            hydrate_recent=2,
        )

        assert [turn["id"] for turn in page.turns] == [
            "user-first", "user-steer", "user-new"]
        assert [turn["prompt"] for turn in page.turns] == [
            "first", "continue", "new"]

    asyncio.run(run())


def test_active_turn_missing_from_official_head_requests_rollout_fallback():
    async def rpc(_method, _params, cwd=None):
        return {
            "data": [_turn(
                "native-old",
                [_user("user-old", "old"), _agent("answer-old", "done")],
            )],
            "nextCursor": None,
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        with pytest.raises(CodexHistoryUnsupported):
            await history.summary_page(
                "thread-active",
                before=None,
                limit=1,
                active_turn_ids={"native-new"},
            )

    asyncio.run(run())


def test_active_head_fallback_retains_exact_official_client_identity():
    async def rpc(_method, _params, cwd=None):
        return {
            "data": [_turn(
                "native-cli-turn",
                [_user(
                    "native-delayed-user",
                    "guide from Remote",
                    client_id="browser-steer-id",
                )],
            )],
            "nextCursor": None,
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        with pytest.raises(CodexHistoryUnsupported):
            await history.summary_page(
                "thread-active",
                before=None,
                limit=1,
                active_turn_ids={"native-active-turn"},
            )
        assert history.take_client_message_identities("thread-active") == (
            (
                "native-cli-turn",
                "native-delayed-user",
                "browser-steer-id",
            ),
        )
        assert history.take_client_message_identities("thread-active") == ()

    asyncio.run(run())


def test_summary_recovers_expired_local_image_from_rollout():
    recovered_calls = []

    async def rpc(_method, _params, cwd=None):
        return {
            "data": [_turn(
                "native-image",
                [{
                    **_user("user-image", "inspect"),
                    "content": [
                        {"type": "text", "text": "inspect"},
                        {"type": "localImage", "path": "/expired/image.png"},
                    ],
                }, _agent("answer-image", "done")],
            )],
            "nextCursor": None,
        }

    async def recover(
        thread_id, native_turn_id, visible_turn_id, user_index,
    ):
        recovered_calls.append(
            (thread_id, native_turn_id, visible_turn_id, user_index))
        return UserMsg(
            msg_id=visible_turn_id,
            prompt="inspect",
            images=[{"media_type": "image/png", "data": _PNG_1X1}],
        )

    async def run():
        history = CodexOfficialHistory(
            64 * 1024, rpc=rpc, recover_user=recover)
        page = await history.summary_page(
            "thread-image", before=None, limit=1)
        assert recovered_calls == [
            ("thread-image", "native-image", "user-image", 0)]
        refs = page.turns[0].get("imageRefs")
        assert refs is not None and len(refs) == 1
        assert refs[0]["width"] == 1 and refs[0]["height"] == 1
        rows = history.summary_events("thread-image", "user-image")
        assert rows is not None
        assert rows[0]["images"][0]["data"] == _PNG_1X1

    asyncio.run(run())


def test_rollout_user_recovery_is_bound_to_the_native_turn(tmp_path):
    rollout = tmp_path / "rollout-images.jsonl"
    rollout.write_text("".join(json.dumps(row) + "\n" for row in [
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-old"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "old"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "native-old"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-image"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{_PNG_1X1}",
                }],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "inspect"},
        },
    ]))

    recovered = codex_history_turn_user(
        str(rollout), "native-image", "user-image")
    assert recovered is not None
    assert recovered.msg_id == "user-image"
    assert recovered.prompt == "inspect"
    assert recovered.images == [{
        "media_type": "image/png",
        "data": _PNG_1X1,
    }]
    assert codex_history_turn_user(
        str(rollout), "missing-turn", "user-image") is None


def test_live_rollout_user_recovery_bounds_the_reverse_search(tmp_path):
    rollout = tmp_path / "rollout-bounded-live-user.jsonl"
    rows = [
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-old"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "old prompt"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "x" * 4096,
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-current"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "current prompt",
            },
        },
    ]
    rollout.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    current = codex_history_turn_user(
        str(rollout),
        "native-current",
        "native-current",
        max_reverse_scan_bytes=512,
    )
    assert current is not None
    assert current.prompt == "current prompt"
    assert codex_history_turn_user(
        str(rollout),
        "native-old",
        "native-old",
        max_reverse_scan_bytes=512,
    ) is None


def test_codex_0147_rollout_uses_official_user_item_identity(tmp_path):
    rollout = tmp_path / "rollout-modern-user.jsonl"
    rollout.write_text("".join(json.dumps(row) + "\n" for row in [
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-modern"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{_PNG_1X1}",
                }],
            },
        },
        {
            "timestamp": "2026-01-01T00:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "native-modern",
                "item": {
                    "id": "user-modern",
                    "clientId": "cli-message-modern",
                    "type": "UserMessage",
                    "content": [{"type": "text", "text": "inspect modern"}],
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "phase": "final_answer",
                "message": "done",
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "native-modern"},
        },
    ]), encoding="utf-8")

    recovered = codex_history_turn_user(
        str(rollout), "native-modern", "user-modern")
    assert recovered is not None
    assert recovered.msg_id == "user-modern"
    assert recovered.client_msg_id == "cli-message-modern"
    assert recovered.prompt == "inspect modern"
    assert recovered.images == [{
        "media_type": "image/png",
        "data": _PNG_1X1,
    }]

    events, _model = codex_translate_history(
        str(rollout), tool_result_max=4096)
    users = [event for event in events if isinstance(event, UserMsg)]
    assert [(event.msg_id, event.client_msg_id, event.prompt) for event in users] == [
        ("user-modern", "cli-message-modern", "inspect modern"),
    ]


def test_rollout_user_recovery_restores_only_a_changed_goal_objective(tmp_path):
    rollout = tmp_path / "rollout-goal.jsonl"
    goal = {
        "threadId": "thread-goal",
        "objective": "证明泰勒展开",
        "status": "active",
        "tokensUsed": 0,
        "timeUsedSeconds": 0,
        "createdAt": 1,
        "updatedAt": 1,
    }
    rollout.write_text("".join(json.dumps(row) + "\n" for row in [
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "thread_goal_updated", "goal": goal}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "goal-new"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "goal-new"}},
        {"timestamp": "2026-01-01T00:01:01Z", "type": "event_msg",
         "payload": {"type": "thread_goal_updated", "goal": {
             **goal, "tokensUsed": 10, "updatedAt": 2,
         }}},
        {"timestamp": "2026-01-01T00:01:02Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "goal-resume"}},
        {"timestamp": "2026-01-01T00:01:03Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "goal-resume"}},
    ]))

    recovered = codex_history_turn_user(
        str(rollout), "goal-new", "goal-new",
    )
    assert recovered is not None
    assert recovered.prompt == "证明泰勒展开"
    assert codex_history_turn_user(
        str(rollout), "goal-resume", "goal-resume",
    ) is None
    batch = codex_history_turn_users(
        str(rollout), ("goal-new", "goal-resume"),
    )
    assert batch.seen_turn_ids == frozenset({"goal-new", "goal-resume"})
    assert set(batch.users) == {"goal-new"}
    assert batch.users["goal-new"].prompt == "证明泰勒展开"


def test_rollout_goal_recovery_accepts_new_objective_after_task_start(tmp_path):
    rollout = tmp_path / "rollout-goal-after-start.jsonl"
    goal = {
        "threadId": "thread-goal",
        "objective": "recover after boundary",
        "status": "active",
        "tokensUsed": 0,
        "timeUsedSeconds": 0,
        "createdAt": 1,
        "updatedAt": 1,
    }
    rollout.write_text("".join(json.dumps(row) + "\n" for row in [
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "goal-after"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "thread_goal_updated", "goal": goal}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "goal-after"}},
    ]))

    recovered = codex_history_turn_user(
        str(rollout), "goal-after", "goal-after",
    )
    assert recovered is not None
    assert recovered.prompt == "recover after boundary"
    batch = codex_history_turn_users(str(rollout), ("goal-after",))
    assert batch.seen_turn_ids == frozenset({"goal-after"})
    assert batch.users["goal-after"].prompt == "recover after boundary"


def test_rollout_goal_recovery_retries_an_open_forward_boundary(tmp_path):
    rollout = tmp_path / "rollout-open-goal.jsonl"
    rollout.write_text(json.dumps({
        "timestamp": "2026-01-01T00:00:01Z",
        "type": "event_msg",
        "payload": {"type": "task_started", "turn_id": "goal-later"},
    }) + "\n")

    initial = codex_history_turn_users(str(rollout), ("goal-later",))
    assert initial.users == {}
    assert initial.seen_turn_ids == frozenset()

    with rollout.open("a") as source:
        for row in [
            {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
             "payload": {"type": "thread_goal_updated", "goal": {
                 "threadId": "thread-goal",
                 "objective": "arrived later",
                 "status": "active",
                 "createdAt": 2,
                 "updatedAt": 2,
             }}},
            {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
             "payload": {"type": "task_complete",
                         "turn_id": "goal-later"}},
        ]:
            source.write(json.dumps(row) + "\n")

    recovered = codex_history_turn_users(str(rollout), ("goal-later",))
    assert recovered.seen_turn_ids == frozenset({"goal-later"})
    assert recovered.users["goal-later"].prompt == "arrived later"


def test_rollout_forward_goal_recovery_does_not_bind_status_or_next_turn(
        tmp_path):
    rollout = tmp_path / "rollout-forward-goal-scope.jsonl"
    original = {
        "threadId": "thread-goal",
        "objective": "same objective",
        "status": "active",
        "createdAt": 1,
        "updatedAt": 1,
    }
    rollout.write_text("".join(json.dumps(row) + "\n" for row in [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg",
         "payload": {"type": "thread_goal_updated", "goal": original}},
        {"timestamp": "2026-01-01T00:00:00.100Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "seed-goal"}},
        {"timestamp": "2026-01-01T00:00:00.200Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "seed-goal"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "status-only"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "thread_goal_updated", "goal": {
             **original, "tokensUsed": 10, "updatedAt": 2,
         }}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "status-only"}},
        {"timestamp": "2026-01-01T00:01:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "next-goal"}},
        {"timestamp": "2026-01-01T00:01:02Z", "type": "event_msg",
         "payload": {"type": "thread_goal_updated", "goal": {
             **original,
             "objective": "next objective",
             "createdAt": 3,
             "updatedAt": 3,
         }}},
        {"timestamp": "2026-01-01T00:01:03Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "next-goal"}},
    ]))

    recovered = codex_history_turn_users(
        str(rollout), ("status-only", "next-goal"),
    )
    assert recovered.seen_turn_ids == frozenset({
        "status-only", "next-goal",
    })
    assert set(recovered.users) == {"next-goal"}
    assert recovered.users["next-goal"].prompt == "next objective"


def test_rollout_user_recovery_selects_later_steer_images(tmp_path):
    rollout = tmp_path / "rollout-steer-images.jsonl"
    rollout.write_text("".join(json.dumps(row) + "\n" for row in [
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-steer"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{_PNG_1X1}",
                    },
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{_PNG_1X1}",
                    },
                ],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "first"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{_PNG_1X1}",
                }],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "steer"},
        },
    ]))

    recovered = codex_history_turn_user(
        str(rollout), "native-steer", "user-steer", 1)
    assert recovered is not None
    assert recovered.msg_id == "user-steer"
    assert recovered.prompt == "steer"
    assert recovered.images is not None
    assert len(recovered.images) == 1


def test_legacy_rollout_user_pair_reuses_live_item_identity(tmp_path):
    rollout = tmp_path / "rollout-legacy-user-item-id.jsonl"
    rows = [
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-turn"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg-native-first",
                "role": "user",
                "content": [{"type": "input_text", "text": "first"}],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "first"},
        },
        # An internal user envelope must not lend its id across an intervening
        # record to the next visible user event.
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg-internal-envelope",
                "role": "user",
                "content": [{"type": "input_text", "text": "internal"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "developer-envelope",
                "role": "developer",
                "content": [{"type": "input_text", "text": "policy"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg-native-steer",
                "role": "user",
                "content": [{"type": "input_text", "text": "steer"}],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "steer"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "native-turn"},
        },
    ]
    encoded_rows = [json.dumps(row) + "\n" for row in rows]
    rollout.write_text("".join(encoded_rows), encoding="utf-8")

    events, _model = codex_translate_history(str(rollout), 64 * 1024)
    users = [event for event in events if isinstance(event, UserMsg)]
    assert [(user.msg_id, user.prompt) for user in users] == [
        ("msg-native-first", "first"),
        ("msg-native-steer", "steer"),
    ]

    steer_offset = sum(len(row.encode()) for row in encoded_rows[:5])
    window_events, _model = codex_translate_history(
        str(rollout), 64 * 1024, start_offset=steer_offset,
    )
    window_users = [
        event for event in window_events if isinstance(event, UserMsg)
    ]
    assert [(user.msg_id, user.prompt) for user in window_users] == [
        ("msg-native-steer", "steer"),
    ]

    # If a bounded window starts after the response item, identity is unknown;
    # fail closed instead of guessing the live id from text or timestamps.
    event_offset = steer_offset + len(encoded_rows[5].encode())
    unpaired_events, _model = codex_translate_history(
        str(rollout), 64 * 1024, start_offset=event_offset,
    )
    unpaired_user = next(
        event for event in unpaired_events if isinstance(event, UserMsg)
    )
    assert unpaired_user.prompt == "steer"
    assert unpaired_user.msg_id != "msg-native-steer"

    first = codex_history_turn_user(
        str(rollout), "native-turn", "native-turn", 0,
    )
    steer = codex_history_turn_user(
        str(rollout), "native-turn", "msg-native-steer", 1,
    )
    assert first is not None and first.msg_id == "msg-native-first"
    assert steer is not None and steer.msg_id == "msg-native-steer"

    start, _end, has_more, _cursor, _boundary = codex_history_window(
        str(rollout), before=None, limit=1, max_bytes=1024 * 1024,
    )
    assert has_more is True
    assert start == steer_offset


def test_native_user_page_cursor_accepts_previous_task_cursor(tmp_path):
    rollout = tmp_path / "rollout-user-cursor-compat.jsonl"
    rows = [
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-old"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "old"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-old"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-current"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg-native-current",
                "role": "user",
                "content": [{"type": "input_text", "text": "current"}],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "current"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-current",
            },
        },
    ]
    encoded_rows = [json.dumps(row) + "\n" for row in rows]
    rollout.write_text("".join(encoded_rows), encoding="utf-8")
    current_offset = sum(len(row.encode()) for row in encoded_rows[:3])

    native_page = codex_history_window(
        str(rollout), before="msg-native-current", limit=1,
        max_bytes=1024 * 1024,
    )
    compatibility_page = codex_history_window(
        str(rollout), before="turn-current", limit=1,
        max_bytes=1024 * 1024,
    )
    assert native_page == compatibility_page
    assert native_page[1] == current_offset


def test_rollout_history_applies_segment_and_native_message_aliases(tmp_path):
    rollout = tmp_path / "rollout-client-aliases.jsonl"
    rows = [
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-turn"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "turn_id": "native-turn",
                "message": "first",
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "native-turn",
                "item": {
                    "type": "userMessage",
                    "id": "native-steer-item",
                    "content": [{"type": "text", "text": "steer"}],
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "native-turn"},
        },
    ]
    encoded_rows = [json.dumps(row) + "\n" for row in rows]
    rollout.write_text("".join(encoded_rows), encoding="utf-8")

    events, _model = codex_translate_history(
        str(rollout),
        64 * 1024,
        segment_client_message_ids={
            ("native-turn", 0): "browser-first",
        },
        client_message_ids={
            "native-steer-item": "browser-steer",
        },
    )
    users = [event for event in events if isinstance(event, UserMsg)]
    assert [user.prompt for user in users] == ["first", "steer"]
    assert [user.client_msg_id for user in users] == [
        "browser-first", "browser-steer",
    ]

    window_events, _model = codex_translate_history(
        str(rollout),
        64 * 1024,
        start_offset=len(encoded_rows[0].encode("utf-8")),
        segment_client_message_ids={
            ("native-turn", 0): "browser-first",
        },
    )
    window_users = [
        event for event in window_events if isinstance(event, UserMsg)
    ]
    assert window_users
    assert all(user.client_msg_id is None for user in window_users)


def test_summary_recovers_images_for_each_visible_steer_segment():
    recovered_calls = []

    def image_user(item_id: str, text: str) -> dict:
        return {
            **_user(item_id, text),
            "content": [
                {"type": "text", "text": text},
                {"type": "localImage", "path": f"/expired/{item_id}.png"},
            ],
        }

    async def rpc(_method, _params, cwd=None):
        return {
            "data": [_turn(
                "native-steer",
                [
                    image_user("user-first", "first"),
                    _agent("answer-first", "first answer"),
                    image_user("user-steer", "steer"),
                    _agent("answer-steer", "steer answer"),
                ],
            )],
            "nextCursor": None,
        }

    async def recover(
        thread_id, native_turn_id, visible_turn_id, user_index,
    ):
        recovered_calls.append((
            thread_id, native_turn_id, visible_turn_id, user_index))
        return UserMsg(
            msg_id=visible_turn_id,
            prompt="recovered",
            images=[{"media_type": "image/png", "data": _PNG_1X1}],
        )

    async def run():
        history = CodexOfficialHistory(
            64 * 1024, rpc=rpc, recover_user=recover)
        page = await history.summary_page(
            "thread-steer", before=None, limit=1)
        assert recovered_calls == [
            ("thread-steer", "native-steer", "user-first", 0),
            ("thread-steer", "native-steer", "user-steer", 1),
        ]
        assert [len(turn.get("imageRefs") or []) for turn in page.turns] == [
            1, 1]

    asyncio.run(run())


def test_in_progress_assistant_only_turn_keeps_native_id_when_completed():
    responses = [
        {
            "data": [_turn(
                "native-auto",
                [_agent("agent-auto", "continuing")],
                status="inProgress",
                completed_at=None,
                duration_ms=None,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-auto",
                [_agent("agent-auto", "done")],
            )],
            "nextCursor": None,
        },
    ]

    async def rpc(_method, _params, cwd=None):
        return responses.pop(0)

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        active = await history.summary_page(
            "thread-auto", before=None, limit=1)
        completed = await history.summary_page(
            "thread-auto", before=None, limit=1)
        assert active.turns[0]["id"] == "native-auto"
        assert active.turns[0]["done"] is False
        assert completed.turns[0]["id"] == "native-auto"
        assert completed.turns[0]["done"] is True

    asyncio.run(run())


def test_summary_rejects_cursor_without_a_visible_page_boundary():
    async def rpc(_method, _params, cwd=None):
        return {"data": [], "nextCursor": "older"}

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        with pytest.raises(CodexHistoryInvalidResponse):
            await history.summary_page(
                "thread-empty", before=None, limit=1)

    asyncio.run(run())


def test_summary_failed_status_is_not_silently_presented_as_success():
    async def rpc(_method, _params, cwd=None):
        assert cwd is None
        return {
            "data": [
                _turn(
                    "native-failed",
                    [_user("user-failed", "run"),
                     _agent("partial", "partial", phase="commentary")],
                    status="failed",
                ),
            ],
            "nextCursor": None,
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-1", before=None, limit=1)
        turn = page.turns[0]
        assert turn["done"] is True
        assert turn["error"] == "该轮未正常结束"
        assert turn["forkPointId"] == "native-failed"

    asyncio.run(run())


@pytest.mark.parametrize("visible_partial", [False, True])
@pytest.mark.parametrize("with_prompt", [False, True])
def test_rollout_capacity_failure_and_followup_keep_separate_outcomes(tmp_path, visible_partial, with_prompt):
    from cc_remote.protocol import Error, TurnEnd
    from cc_remote.wrapper.history_store import materialize_history_turns

    path = tmp_path / "rollout.jsonl"
    payloads = [
        {"type": "task_started", "turn_id": "failed-native"},
    ]
    if with_prompt:
        payloads.append({"type": "user_message", "message": "deploy"})
    if visible_partial:
        payloads.append({"type": "agent_message", "message": "Checking build.", "phase": "commentary"})
    payloads.extend([
        {"type": "task_complete", "turn_id": "failed-native", "last_agent_message": None,
         "duration_ms": 337204, "error": {"message": "Selected model is at capacity.",
                                         "codex_error_info": "server_overloaded"}},
        {"type": "task_started", "turn_id": "followup-native"},
        {"type": "agent_message", "message": "Continuing checks.", "phase": "commentary"},
    ])

    def translated():
        path.write_text("".join(json.dumps({
            "type": "event_msg", "payload": payload,
            "timestamp": f"2026-09-09T14:04:{index:02d}Z",
        }) + "\n" for index, payload in enumerate(payloads)))
        events, _ = codex_translate_history(str(path), 8000)
        turns = materialize_history_turns([event.model_dump(mode="json") for event in events])
        return events, turns

    events, turns = translated()
    assert len(turns) == 2
    assert turns[0]["forkPointId"] == "failed-native"
    assert turns[0]["error"] == "当前模型繁忙，请稍后重试或切换模型。"
    assert turns[0]["done"] is True
    assert turns[1]["forkPointId"] == "followup-native"
    assert turns[1]["id"] == "followup-native"
    assert turns[1]["done"] is False
    assert not turns[1].get("error")
    assert len([event for event in events if isinstance(event, Error)]) == 1
    terminal = next(event for event in events if isinstance(event, TurnEnd))
    assert terminal.result.is_error and terminal.result.subtype == "error"
    payloads.append({"type": "task_complete", "turn_id": "followup-native", "last_agent_message": "Done."})
    _, completed = translated()
    assert completed[1]["id"] == turns[1]["id"]
    assert completed[0]["error"] == turns[0]["error"]
    assert completed[1]["done"] and not completed[1].get("error")


def test_official_capacity_summary_retains_reason_without_provider_details():
    async def rpc(_method, _params, cwd=None):
        return {"data": [_turn("failed-native", [_user("user", "deploy")], status="failed",
                              error={"codexErrorInfo": "serverOverloaded", "message": "SECRET"})],
                "nextCursor": None}

    async def run():
        page = await CodexOfficialHistory(64 * 1024, rpc=rpc).summary_page("session", before=None, limit=1)
        assert page.turns[0]["error"] == "当前模型繁忙，请稍后重试或切换模型。"
        assert "SECRET" not in json.dumps(page.turns)

    asyncio.run(run())


def test_summary_cursor_is_session_bound_and_unknown_cursor_is_rejected():
    responses = [
        {
            "data": [_turn(
                "native-2", [_user("user-2", "two"), _agent("a-2", "2")])],
            "nextCursor": "opaque-older",
        },
        {
            "data": [_turn(
                "native-1", [_user("user-1", "one"), _agent("a-1", "1")])],
            "nextCursor": None,
        },
    ]
    calls = []

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        return responses.pop(0)

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page("thread-a", before=None, limit=1)
        older = await history.summary_page(
            "thread-a", before="user-2", limit=1)
        assert older.oldest_id == "user-1"
        assert calls[-1][1]["cursor"] == "opaque-older"

        with pytest.raises(CodexHistoryCursorError):
            await history.summary_page(
                "thread-b", before="user-2", limit=1)
        with pytest.raises(CodexHistoryCursorError):
            await history.summary_page(
                "thread-a", before="unknown", limit=1)

    asyncio.run(run())


def test_summary_rejects_dirty_or_duplicate_official_data():
    responses = [
        {"data": "not-a-list", "nextCursor": None},
        {
            "data": [
                _turn(
                    "native-1",
                    [_user("same-item", "one"), _agent("same-item", "answer")],
                ),
            ],
            "nextCursor": None,
        },
    ]

    async def rpc(_method, _params, cwd=None):
        return responses.pop(0)

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        with pytest.raises(CodexHistoryInvalidResponse):
            await history.summary_page(
                "thread-1", before=None, limit=1)
        with pytest.raises(CodexHistoryInvalidResponse):
            await history.summary_page(
                "thread-1", before=None, limit=1)

    asyncio.run(run())


def test_detail_prefers_items_list_and_preserves_rich_official_items():
    summary = {
        "data": [
            _turn(
                "native-1",
                [_user("user-1", "inspect"), _agent("answer-1", "done")],
            ),
        ],
        "nextCursor": None,
    }
    full_items = [
        _user("user-1", "inspect"),
        {
            "type": "reasoning",
            "id": "reason-1",
            "summary": ["checking"],
            "content": [],
        },
        {
            "type": "commandExecution",
            "id": "command-1",
            "command": "pwd",
            "commandActions": [],
            "cwd": "/repo",
            "status": "completed",
            "aggregatedOutput": "/repo\n",
            "exitCode": 0,
            "durationMs": 12,
        },
        {"type": "contextCompaction", "id": "compact-1"},
        {
            "type": "subAgentActivity",
            "id": "agent-1",
            "agentPath": "worker",
            "agentThreadId": "child-thread",
            "kind": "interacted",
        },
        _agent("answer-1", "done"),
    ]
    calls = []

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        if method == "thread/turns/list":
            return summary
        assert method == "thread/items/list"
        return {
            "data": [
                {"turnId": "native-1", "item": item}
                for item in full_items
            ],
            "nextCursor": None,
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-1", before=None, limit=1)
        events = await history.turn_events("thread-1", "user-1")
        types = [event["type"] for event in events]
        assert types[0] == "user_msg"
        assert {"tool_use", "tool_result", "process", "turn_end"} <= set(types)
        assert any(
            event.get("item_id") == "reason-1"
            and event.get("kind") == "reasoning"
            for event in events
        )
        assert any(
            event.get("item_id") == "compact-1"
            and event.get("kind") == "compaction"
            for event in events
        )
        assert any(
            event.get("item_id") == "agent-1"
            and event.get("parent_id") == "child-thread"
            for event in events
        )
        assert calls[-1] == ("thread/items/list", {
            "threadId": "thread-1",
            "turnId": "native-1",
            "cursor": None,
            "limit": 1024,
            "sortDirection": "asc",
        })
        call_count = len(calls)
        assert await history.turn_events("thread-1", "user-1") == events
        assert len(calls) == call_count

    asyncio.run(run())


def test_detail_falls_back_from_items_list_to_exact_full_turn():
    summary = {
        "data": [
            _turn(
                "native-new",
                [_user("user-new", "new"), _agent("a-new", "new")],
            ),
            _turn(
                "native-target",
                [_user("user-target", "target"), _agent("a-target", "done")],
            ),
        ],
        "nextCursor": "older",
    }
    calls = []

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        if len(calls) == 1:
            return summary
        if method == "thread/items/list":
            raise CodexRpcRejected(
                "codex app-server error -32601: not supported yet",
                code=-32601,
            )
        if params["itemsView"] == "notLoaded":
            assert params["cursor"] is None
            return {
                "data": [_turn(
                    "native-new", [], items_view="notLoaded")],
                "nextCursor": "after-new",
            }
        assert params["itemsView"] == "full"
        assert params["cursor"] == "after-new"
        return {
            "data": [_turn(
                "native-target",
                [_user("user-target", "target"), _agent("a-target", "done")],
                items_view="full",
            )],
            "nextCursor": "after-target",
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-1", before=None, limit=2)
        events = await history.turn_events(
            "thread-1", "user-target")
        assert events[0]["msg_id"] == "user-target"
        assert events[-1]["turn_id"] == "native-target"
        assert history.turn_detail_source(
            "thread-1", "user-target") == "full"
        assert [method for method, _params in calls] == [
            "thread/turns/list",
            "thread/items/list",
            "thread/turns/list",
            "thread/turns/list",
        ]

    asyncio.run(run())


def test_item_pages_reject_empty_nonterminal_page_immediately():
    calls = 0

    async def rpc(method, params, cwd=None):
        nonlocal calls
        assert method == "thread/items/list"
        calls += 1
        return {"data": [], "nextCursor": f"cursor-{calls}"}

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        with pytest.raises(
            CodexHistoryInvalidResponse,
            match="empty before its terminal cursor",
        ):
            await history._items_for_turn("thread-1", "native-1")
        assert calls == 1

    asyncio.run(run())


def test_item_pages_never_issue_more_than_sixteen_rpcs():
    calls = 0

    async def rpc(method, params, cwd=None):
        nonlocal calls
        assert method == "thread/items/list"
        calls += 1
        item = _agent(f"agent-{calls}", f"page {calls}")
        return {
            "data": [{"turnId": "native-1", "item": item}],
            "nextCursor": f"cursor-{calls}",
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        with pytest.raises(
            CodexHistoryInvalidResponse,
            match="exceeded its page limit",
        ):
            await history._items_for_turn("thread-1", "native-1")
        assert calls == 16

    asyncio.run(run())


def test_detail_items_list_is_recorded_as_complete_official_source():
    async def rpc(method, params, cwd=None):
        if method == "thread/turns/list":
            return {
                "data": [_turn(
                    "native-1",
                    [_user("user-1", "inspect"), _agent("a-1", "done")],
                )],
                "nextCursor": None,
            }
        assert method == "thread/items/list"
        return {
            "data": [
                {"turnId": "native-1", "item": item}
                for item in [
                    _user("user-1", "inspect"),
                    {
                        "type": "commandExecution",
                        "id": "command-1",
                        "command": "pwd",
                        "commandActions": [],
                        "cwd": "/repo",
                        "status": "completed",
                        "aggregatedOutput": "/repo\n",
                        "exitCode": 0,
                        "durationMs": 12,
                    },
                    _agent("a-1", "done"),
                ]
            ],
            "nextCursor": None,
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page("thread-1", before=None, limit=1)
        await history.turn_events("thread-1", "user-1")
        assert history.turn_detail_source("thread-1", "user-1") == "items"
        history.invalidate_thread("thread-1")
        assert history.turn_detail_source("thread-1", "user-1") is None

    asyncio.run(run())


def test_detail_oversized_item_page_uses_exact_full_turn():
    calls = 0

    async def rpc(method, params, cwd=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "data": [_turn(
                    "native-1",
                    [_user("user-1", "one"), _agent("a-1", "done")],
                )],
                "nextCursor": None,
            }
        if method == "thread/items/list":
            raise CodexRpcResponseTooLarge("oversized item page")
        assert params["itemsView"] == "full"
        return {
            "data": [_turn(
                "native-1",
                [_user("user-1", "one"), _agent("a-1", "done")],
                items_view="full",
            )],
            "nextCursor": None,
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-1", before=None, limit=1)
        events = await history.turn_events("thread-1", "user-1")
        assert events[-1]["turn_id"] == "native-1"

    asyncio.run(run())


def test_detail_oversized_full_turn_requests_rollout_fallback():
    calls = 0

    async def rpc(method, params, cwd=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "data": [_turn(
                    "native-1",
                    [_user("user-1", "one"), _agent("a-1", "done")],
                )],
                "nextCursor": None,
            }
        raise CodexRpcResponseTooLarge(
            f"oversized {method} response")

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-1", before=None, limit=1)
        with pytest.raises(CodexHistoryUnsupported):
            await history.turn_events("thread-1", "user-1")

    asyncio.run(run())


def test_detail_falls_back_to_rollout_only_when_both_official_apis_unsupported():
    calls = []

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        if len(calls) == 1:
            return {
                "data": [_turn(
                    "native-1",
                    [_user("user-1", "one"), _agent("a-1", "done")],
                )],
                "nextCursor": None,
            }
        raise CodexRpcRejected(
            "codex app-server error -32601: unsupported",
            code=-32601,
        )

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-1", before=None, limit=1)
        with pytest.raises(CodexHistoryUnsupported):
            await history.turn_events("thread-1", "user-1")
        fallback = history.rollout_fallback("thread-1", "user-1")
        assert fallback.before is None
        assert fallback.limit == 1
        assert fallback.native_turn_id == "native-1"

    asyncio.run(run())


def test_detail_auth_rejection_is_not_downgraded_to_rollout():
    async def rpc(method, params, cwd=None):
        if method == "thread/turns/list":
            return {
                "data": [_turn(
                    "native-1",
                    [_user("user-1", "one"), _agent("a-1", "done")],
                )],
                "nextCursor": None,
            }
        raise CodexRpcRejected(
            "codex app-server error -32001: unauthorized",
            code=-32001,
        )

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-1", before=None, limit=1)
        with pytest.raises(CodexRpcRejected) as rejected:
            await history.turn_events("thread-1", "user-1")
        assert rejected.value.code == -32001

    asyncio.run(run())


def test_multi_user_detail_selects_the_requested_full_segment_after_summary_collapse():
    calls = 0

    async def rpc(method, params, cwd=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "data": [_turn(
                    "native-1",
                    [
                        _user("user-1", "first"),
                        _agent("a-2", "second"),
                    ],
                )],
                "nextCursor": None,
            }
        if method == "thread/items/list":
            return {
                "data": [
                    {"turnId": "native-1", "item": _user("user-1", "first")},
                    {"turnId": "native-1", "item": _agent("a-1", "first")},
                    {"turnId": "native-1", "item": _user("user-2", "second")},
                    {"turnId": "native-1", "item": _agent("a-2", "second")},
                ],
                "nextCursor": None,
            }
        raise AssertionError("unexpected RPC")

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-1", before=None, limit=1)
        events = await history.turn_events("thread-1", "user-1")
        assert events[0]["msg_id"] == "user-1"
        assert any(
            event.get("message_id") == "a-1"
            for event in events
        )
        assert not any(
            event.get("message_id") == "a-2"
            for event in events
        )

    asyncio.run(run())
