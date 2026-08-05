"""Zero-token unit tests for on-demand bulk history (GetHistory/History) and the
cursor-aware hello (fresh snapshots, delta replay on reconnect). No relay/wrapper/
cc/model — these exercise the wrapper handlers directly with a stub transport.

Run: ./.venv/bin/python -m pytest tests/test_history.py -q
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from cc_remote.protocol import (
    serialize, deserialize,
    GetHistory, GetHistoryImage, GetTurnDetail, History, HistoryImage,
    TurnDetail, HistoryInvalidated,
    UserMsg, AssistantMsgStart, AssistantMsgEnd, Delta, ProcessEvent,
    TurnEnd, TurnResult, Error, is_downstream,
)
from cc_remote.wrapper import machine as mm
from cc_remote.wrapper.codex_history import (
    CodexHistoryPage,
    CodexHistoryUnsupported,
)
from cc_remote.wrapper.codex_rpc import CodexRpcRejected
from cc_remote.wrapper.codex_stream import CodexHistoryImageView
from cc_remote.wrapper.history_store import (
    HistoryIndexStore,
    HistorySourceFingerprint,
    MaterializedHistoryPage,
    history_image_id,
    materialize_history_turns,
)
from cc_remote.wrapper.stream import (
    StreamTranslator,
    transcript_compact_history_page,
    transcript_compact_snapshot,
    last_assistant_model,
    transcript_compact_main_chain,
    transcript_internal_user_events,
    transcript_timestamps,
    translate_history,
)
from tests.test_multisession import _mk_machine, _mk_ctx


def test_claude_sdk_prompt_id_survives_history_as_exact_client_alias(
    monkeypatch, tmp_path,
):
    """Switch-back History must reconcile one SDK prompt with its live row.

    Claude stores the transcript UUID as ``uuid`` and the browser query id as
    ``promptId``.  The SDK's SessionMessage projection drops promptId, so the
    timestamp side read is also the source of this exact, non-textual alias.
    """
    session_id = "fa800ca3-18e3-4391-b401-a33fe52e2f56"
    transcript_id = "2259073b-7676-455f-b7b0-b9b3892dbe93"
    client_id = "6b09ee37-f861-4422-b98a-21f509c951b0"
    transcript = tmp_path / f"{session_id}.jsonl"
    transcript.write_text(json.dumps({
        "type": "user",
        "uuid": transcript_id,
        "promptId": client_id,
        "promptSource": "sdk",
        "timestamp": "2026-08-02T09:09:16.263Z",
        "message": {
            "role": "user",
            "content": "我又改完了一版，你看看还有没得问题？",
        },
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "cc_remote.wrapper.stream.transcript_path",
        lambda _session_id: str(transcript),
    )

    timestamps = transcript_timestamps(session_id)
    events = translate_history([
        SimpleNamespace(
            type="user",
            uuid=transcript_id,
            session_id=session_id,
            message={
                "role": "user",
                "content": "我又改完了一版，你看看还有没得问题？",
            },
            parent_tool_use_id=None,
        ),
    ], 10_000, timestamps=timestamps)

    user = next(event for event in events if isinstance(event, UserMsg))
    assert user.msg_id == transcript_id
    assert user.client_msg_id == client_id


def test_claude_repeated_prompt_text_keeps_distinct_prompt_identities(
    monkeypatch, tmp_path,
):
    session_id = "fa800ca3-18e3-4391-b401-a33fe52e2f56"
    transcript_ids = [
        "2259073b-7676-455f-b7b0-b9b3892dbe93",
        "3259073b-7676-455f-b7b0-b9b3892dbe93",
    ]
    client_ids = [
        "6b09ee37-f861-4422-b98a-21f509c951b0",
        "7b09ee37-f861-4422-b98a-21f509c951b0",
    ]
    transcript = tmp_path / f"{session_id}.jsonl"
    transcript.write_text("".join(
        json.dumps({
            "type": "user",
            "uuid": transcript_id,
            "promptId": client_id,
            "promptSource": "sdk",
            "timestamp": f"2026-08-02T09:09:{16 + index:02d}.263Z",
            "message": {"role": "user", "content": "继续"},
        }) + "\n"
        for index, (transcript_id, client_id) in enumerate(zip(
            transcript_ids, client_ids, strict=True))
    ), encoding="utf-8")
    monkeypatch.setattr(
        "cc_remote.wrapper.stream.transcript_path",
        lambda _session_id: str(transcript),
    )
    messages = [
        SimpleNamespace(
            type="user",
            uuid=transcript_id,
            session_id=session_id,
            message={"role": "user", "content": "继续"},
            parent_tool_use_id=None,
        )
        for transcript_id in transcript_ids
    ]

    users = [
        event for event in translate_history(
            messages, 10_000, timestamps=transcript_timestamps(session_id))
        if isinstance(event, UserMsg)
    ]
    assert [(user.msg_id, user.client_msg_id) for user in users] == list(zip(
        transcript_ids, client_ids, strict=True))


def test_codex_image_view_supplement_keeps_official_detail_and_deduplicates():
    official = [
        {"type": "user_msg", "msg_id": "user-1", "prompt": "inspect"},
        {
            "type": "process",
            "item_id": "reason-1",
            "kind": "reasoning",
            "phase": "end",
            "status": "succeeded",
            "title": "思考",
        },
        {
            "type": "tool_use",
            "message_id": "message-1",
            "tool_use_id": "call-image-1",
            "tool": "view_image",
            "input": {"path": "/tmp/chart.png"},
        },
        {
            "type": "tool_result",
            "tool_use_id": "call-image-1",
            "content": "data:image/png;base64,SHOULD_NOT_SURVIVE",
            "is_error": False,
        },
        {
            "type": "process",
            "item_id": "command-after-image",
            "kind": "command",
            "phase": "end",
            "status": "succeeded",
            "title": "运行命令",
        },
        {
            "type": "assistant_msg_start",
            "message_id": "final-1",
            "channel": "final",
        },
        {
            "type": "delta",
            "message_id": "final-1",
            "channel": "final",
            "text": "done",
        },
        {
            "type": "assistant_msg_end",
            "message_id": "final-1",
            "channel": "final",
        },
        {
            "type": "turn_end",
            "turn_id": "native-1",
            "result": {
                "subtype": "success",
                "duration_ms": 1,
                "is_error": False,
            },
        },
    ]
    image_event = ProcessEvent(
        item_id="fc-image-1",
        kind="server_tool",
        phase="end",
        status="succeeded",
        turn_id="native-1",
        title="查看图片",
        tool="view_image",
        input={
            "file_path": "/tmp/chart.png",
            "history_image": {
                "image_id": "img-123",
                "media_type": "image/png",
                "width": 1,
                "height": 1,
                "byte_size": 68,
            },
        },
    )
    view = CodexHistoryImageView(
        call_id="call-image-1",
        event=image_event,
        next_item_id="command-after-image",
    )

    merged = mm._merge_codex_history_image_views(official, (view,))

    assert any(row.get("item_id") == "reason-1" for row in merged)
    image_rows = [
        row for row in merged
        if row.get("type") == "process" and row.get("tool") == "view_image"
    ]
    assert len(image_rows) == 1
    assert image_rows[0]["item_id"] == "fc-image-1"
    assert all(row.get("tool_use_id") != "call-image-1" for row in merged)
    assert "SHOULD_NOT_SURVIVE" not in json.dumps(merged)
    assert merged.index(image_rows[0]) < next(
        index for index, row in enumerate(merged)
        if row.get("item_id") == "command-after-image"
    )
    assert merged.index(image_rows[0]) < next(
        index for index, row in enumerate(merged)
        if row.get("type") == "assistant_msg_start"
        and row.get("channel") == "final"
    )

    official_with_image = [
        official[0],
        official[1],
        {
            **image_event.model_dump(mode="json"),
            "input": {"file_path": "/tmp/chart.png"},
        },
        *official[2:],
    ]
    deduplicated = mm._merge_codex_history_image_views(
        official_with_image, (view,))
    official_image_rows = [
        row for row in deduplicated
        if row.get("type") == "process" and row.get("tool") == "view_image"
    ]
    assert len(official_image_rows) == 1
    assert official_image_rows[0]["input"]["history_image"]["image_id"] == "img-123"
    assert all(
        row.get("tool_use_id") != "call-image-1"
        for row in deduplicated
    )

    official_without_image_shell = [
        row for row in official
        if row.get("tool_use_id") != "call-image-1"
    ]
    anchored = mm._merge_codex_history_image_views(
        official_without_image_shell, (view,))
    anchored_image = next(
        row for row in anchored
        if row.get("type") == "process"
        and row.get("tool") == "view_image"
    )
    assert anchored.index(anchored_image) < next(
        index for index, row in enumerate(anchored)
        if row.get("item_id") == "command-after-image"
    )


def test_codex_turn_detail_lazily_recovers_missing_official_image_view(
    monkeypatch, tmp_path,
):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 32), (70, 90, 130)).save(buffer, "PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    rollout = tmp_path / "rollout-image-view.jsonl"
    rollout.write_text("".join(json.dumps(row) + "\n" for row in [
        {"timestamp": "2026-07-30T06:40:00Z", "type": "session_meta",
         "payload": {"id": "session-image-view"}},
        {"timestamp": "2026-07-30T06:40:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "native-1"}},
        {"timestamp": "2026-07-30T06:40:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "turn_id": "native-1",
                     "message": "inspect"}},
        {"timestamp": "2026-07-30T06:40:03Z", "type": "response_item",
         "payload": {
             "type": "function_call", "id": "fc-image-1",
             "name": "view_image", "call_id": "call-image-1",
             "arguments": '{"path":"/tmp/chart.png","detail":"original"}',
             "internal_chat_message_metadata_passthrough": {
                 "turn_id": "native-1",
             },
         }},
        {"timestamp": "2026-07-30T06:40:04Z", "type": "response_item",
         "payload": {
             "type": "function_call_output", "call_id": "call-image-1",
             "output": [{
                 "type": "input_image",
                 "image_url": f"data:image/png;base64,{encoded}",
                 "detail": "original",
             }],
             "internal_chat_message_metadata_passthrough": {
                 "turn_id": "native-1",
             },
         }},
        {"timestamp": "2026-07-30T06:40:05Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "native-1"}},
    ]), encoding="utf-8")
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(rollout))
    image_view_reads = 0
    real_image_view_reader = mm.codex_history_image_views

    def counted_image_view_reader(*args, **kwargs):
        nonlocal image_view_reads
        image_view_reads += 1
        return real_image_view_reader(*args, **kwargs)

    monkeypatch.setattr(
        mm, "codex_history_image_views", counted_image_view_reader)
    official = (
        {"type": "user_msg", "msg_id": "user-1", "prompt": "inspect"},
        ProcessEvent(
            item_id="official-plan",
            kind="plan",
            phase="end",
            status="succeeded",
            turn_id="native-1",
            title="计划",
        ).model_dump(mode="json"),
        {"type": "assistant_msg_start", "message_id": "answer-1",
         "channel": "final"},
        {"type": "delta", "message_id": "answer-1",
         "channel": "final", "text": "done"},
        {"type": "assistant_msg_end", "message_id": "answer-1",
         "channel": "final"},
        {"type": "turn_end", "turn_id": "native-1",
         "result": {"subtype": "success", "duration_ms": 1,
                    "is_error": False}},
    )

    class OfficialHistory:
        async def turn_events(self, _sid, _turn_id):
            return official

        def rollout_fallback(self, _sid, _turn_id):
            return SimpleNamespace(
                before=None,
                limit=4,
                native_turn_id="native-1",
                segment_index=0,
                segment_count=1,
            )

        def summary_events(self, _sid, _turn_id):
            return None

    async def go():
        machine, _ = _mk_machine()
        machine._history_index = HistoryIndexStore(tmp_path / "state-image-view")
        machine._codex_history = OfficialHistory()
        ctx = _mk_ctx("session-image-view", "session-image-view")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        revision = machine._history_revision("session-image-view")

        detail = await machine._handle_get_turn_detail(SimpleNamespace(
            session_id="session-image-view",
            turn_id="user-1",
            client_id="client-1",
            revision=revision,
            before=None,
            limit=192,
        ))
        assert any(
            event.get("item_id") == "official-plan"
            for event in detail.events
        )
        image_events = [
            event for event in detail.events
            if event.get("type") == "process"
            and event.get("tool") == "view_image"
        ]
        assert len(image_events) == 1
        wire = detail.model_dump_json()
        assert encoded not in wire
        image_id = image_events[0]["input"]["history_image"]["image_id"]

        image = await machine._handle_get_history_image(SimpleNamespace(
            session_id="session-image-view",
            turn_id="user-1",
            image_id=image_id,
            variant="full",
            request_id="image-request",
            client_id="client-1",
            revision=revision,
        ))
        assert image.error is None and image.media_type == "image/png"
        assert image.width == 64 and image.height == 32
        assert base64.b64decode(image.data) == buffer.getvalue()
        assert image_view_reads == 1

        with rollout.open("a", encoding="utf-8") as output:
            output.write(json.dumps({
                "timestamp": "2026-07-30T06:41:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": "native-2",
                },
            }) + "\n")
        appended_detail = await machine._handle_get_turn_detail(
            SimpleNamespace(
                session_id="session-image-view",
                turn_id="user-1",
                client_id="client-1",
                revision=revision,
                before=None,
                limit=192,
            )
        )
        assert any(
            event.get("type") == "process"
            and event.get("tool") == "view_image"
            for event in appended_detail.events
        )
        assert image_view_reads == 1, (
            "a validated append must reuse a completed turn's supplement"
        )

        machine._history_index.invalidate_session("session-image-view")
        rehydrated = await machine._handle_get_history_image(SimpleNamespace(
            session_id="session-image-view",
            turn_id="user-1",
            image_id=image_id,
            variant="full",
            request_id="image-request-after-eviction",
            client_id="client-1",
            revision=revision,
        ))
        assert rehydrated.error is None
        assert base64.b64decode(rehydrated.data) == buffer.getvalue()
        assert image_view_reads == 2, (
            "an evicted image asset must be rehydrated from the rollout"
        )
        assert any(
            key[0] == "session-image-view"
            for key in machine._codex_history_image_views
        )
        machine._bump_history_revision("session-image-view")
        assert not any(
            key[0] == "session-image-view"
            for key in machine._codex_history_image_views
        )

    asyncio.run(go())


def test_requested_codex_summary_uses_official_turns_without_rollout_parse(
    monkeypatch, tmp_path,
):
    rollout = tmp_path / "official-summary.jsonl"
    rollout.write_text(
        '{"type":"session_meta","payload":{"id":"official-summary"}}\n')
    monkeypatch.setattr(
        mm, "codex_rollout_path", lambda _sid: str(rollout))
    monkeypatch.setattr(
        mm,
        "codex_translate_history",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("official summary parsed the rollout")),
    )
    events = (
        UserMsg(
            sid="official-summary",
            msg_id="user-1",
            client_msg_id="client-1",
            prompt="hello",
            ts=100,
        ).model_dump(mode="json"),
        AssistantMsgStart(
            sid="official-summary",
            message_id="answer-1",
            channel="final",
            ts=100,
        ).model_dump(mode="json"),
        Delta(
            sid="official-summary",
            message_id="answer-1",
            channel="final",
            text="world",
            ts=100,
        ).model_dump(mode="json"),
        AssistantMsgEnd(
            sid="official-summary",
            message_id="answer-1",
            channel="final",
            ts=100,
        ).model_dump(mode="json"),
        TurnEnd(
            sid="official-summary",
            turn_id="native-1",
            result=TurnResult(
                subtype="success", duration_ms=1000, is_error=False),
            ts=101,
        ).model_dump(mode="json"),
    )

    class Official:
        async def summary_page(
            self, sid, *, before, limit, include_live_detail=False,
            active_turn_ids=frozenset(), hydrate_recent=0,
        ):
            assert (
                sid, before, limit, include_live_detail, active_turn_ids,
                hydrate_recent,
            ) == ("official-summary", None, 4, False, frozenset(), 2)
            return CodexHistoryPage(
                events=events,
                turns=materialize_history_turns(events),
                has_more=True,
                oldest_id="user-1",
                newest_id="user-1",
            )

    async def run():
        machine, _transport = _mk_machine()
        machine._codex_history = Official()
        ctx = _mk_ctx("official-summary", "official-summary")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx

        history = await machine._build_requested_history(
            "official-summary",
            before=None,
            limit=4,
            cwd=ctx.cwd,
            detail="summary",
        )

        assert history.authoritative is True
        assert history.detail == "summary"
        assert [turn.id for turn in history.turns] == ["user-1"]
        assert history.turns[0].clientMsgId == "client-1"
        assert history.turns[0].forkPointId == "native-1"
        assert history.has_more is True
        assert all(row["type"] in {"model", "effort"}
                   for row in history.events)

    asyncio.run(run())


def test_requested_codex_summary_binds_exact_active_native_turn_ids(
    monkeypatch, tmp_path,
):
    rollout = tmp_path / "active-summary.jsonl"
    rollout.write_text(
        '{"type":"session_meta","payload":{"id":"active-summary"}}\n')
    monkeypatch.setattr(
        mm, "codex_rollout_path", lambda _sid: str(rollout))
    seen = []

    class Official:
        async def summary_page(self, sid, **kwargs):
            seen.append((sid, kwargs))
            return CodexHistoryPage(
                events=(),
                turns=(),
                has_more=False,
                oldest_id=None,
                newest_id=None,
            )

    async def run():
        machine, _transport = _mk_machine()
        machine._codex_history = Official()
        ctx = _mk_ctx("active-summary", "active-summary")
        ctx.engine = "codex"
        ctx.state = "running"
        ctx.codex_owned_turn_id = "owned-turn"
        ctx.codex_spontaneous_turn_id = "goal-turn"
        ctx.sdk = SimpleNamespace(turn_id="managed-turn")
        machine.sessions[ctx.key] = ctx
        machine._watch["active-summary"] = {
            "engine": "codex",
            "active_external_turns": {"desktop-turn": 1.0},
            "takeover_pending": None,
        }

        history = await machine._build_requested_history(
            "active-summary",
            before=None,
            limit=4,
            cwd=ctx.cwd,
            detail="summary",
        )

        assert history.in_progress is True
        assert seen == [("active-summary", {
            "before": None,
            "limit": 4,
            "include_live_detail": True,
            "active_turn_ids": {"desktop-turn"},
            "hydrate_recent": 2,
        })]
        seen.clear()
        machine._watch["active-summary"]["active_external_turns"] = {}
        await machine._build_requested_history(
            "active-summary",
            before=None,
            limit=4,
            cwd=ctx.cwd,
            detail="summary",
        )
        assert seen == [("active-summary", {
            "before": None,
            "limit": 4,
            "include_live_detail": True,
            "active_turn_ids": {"owned-turn"},
            "hydrate_recent": 2,
        })]

    asyncio.run(run())


def test_requested_codex_summary_falls_back_only_for_unsupported_capability(
    monkeypatch, tmp_path,
):
    rollout = tmp_path / "unsupported-summary.jsonl"
    rollout.write_text(
        '{"type":"session_meta","payload":{"id":"unsupported-summary"}}\n')
    monkeypatch.setattr(
        mm, "codex_rollout_path", lambda _sid: str(rollout))

    class Unsupported:
        async def summary_page(self, *_args, **_kwargs):
            raise CodexHistoryUnsupported("old app-server")

    async def run():
        machine, _transport = _mk_machine()
        machine._codex_history = Unsupported()
        ctx = _mk_ctx("unsupported-summary", "unsupported-summary")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        fallback_calls = []

        async def fallback(*args, **kwargs):
            fallback_calls.append((args, kwargs))
            return History(
                session_id="unsupported-summary",
                revision=machine._history_revision("unsupported-summary"),
                detail="summary",
            )

        monkeypatch.setattr(machine, "_build_history", fallback)
        history = await machine._build_requested_history(
            "unsupported-summary",
            before=None,
            limit=4,
            cwd=None,
            detail="summary",
        )
        assert history.error is None
        assert len(fallback_calls) == 1

    asyncio.run(run())


def test_requested_codex_summary_does_not_hide_auth_failure_with_rollout(
    monkeypatch, tmp_path,
):
    rollout = tmp_path / "auth-summary.jsonl"
    rollout.write_text(
        '{"type":"session_meta","payload":{"id":"auth-summary"}}\n')
    monkeypatch.setattr(
        mm, "codex_rollout_path", lambda _sid: str(rollout))

    class Rejected:
        async def summary_page(self, *_args, **_kwargs):
            raise CodexRpcRejected(
                "codex app-server error -32001: unauthorized",
                code=-32001,
            )

    async def run():
        machine, _transport = _mk_machine()
        machine._codex_history = Rejected()
        ctx = _mk_ctx("auth-summary", "auth-summary")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        monkeypatch.setattr(
            machine,
            "_build_history",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("auth failure fell back to rollout")),
        )

        history = await machine._build_requested_history(
            "auth-summary",
            before=None,
            limit=4,
            cwd=None,
            detail="summary",
        )
        assert history.authoritative is False
        assert history.error == "历史暂时不可用，请稍后重试"
        assert history.turns == []

    asyncio.run(run())


def test_codex_turn_detail_uses_official_items_without_history_index(
    monkeypatch, tmp_path,
):
    rollout = tmp_path / "official-detail.jsonl"
    rollout.write_text(
        '{"type":"session_meta","payload":{"id":"official-detail"}}\n')
    monkeypatch.setattr(
        mm, "codex_rollout_path", lambda _sid: str(rollout))
    rows = (
        UserMsg(
            sid="official-detail",
            msg_id="user-1",
            prompt="inspect",
        ).model_dump(mode="json"),
        ProcessEvent(
            sid="official-detail",
            item_id="process-1",
            kind="compaction",
            phase="end",
            status="succeeded",
            title="压缩上下文",
        ).model_dump(mode="json"),
        TurnEnd(
            sid="official-detail",
            turn_id="native-1",
            result=TurnResult(
                subtype="success", duration_ms=1, is_error=False),
        ).model_dump(mode="json"),
    )

    class Official:
        async def turn_events(self, sid, turn_id):
            assert (sid, turn_id) == ("official-detail", "user-1")
            return rows

    async def run():
        machine, transport = _mk_machine()
        machine._history_index = None
        machine._codex_history = Official()
        ctx = _mk_ctx("official-detail", "official-detail")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx

        detail = await machine._handle_get_turn_detail(SimpleNamespace(
            session_id="official-detail",
            turn_id="user-1",
            revision=machine._history_revision("official-detail"),
            client_id="client-1",
            before=None,
            limit=192,
        ))
        assert detail.authoritative is True
        assert detail.error is None
        assert any(
            row.get("item_id") == "process-1"
            for row in detail.events
        )
        assert transport.sent[-1] == detail

    asyncio.run(run())


def test_codex_full_turn_detail_uses_exact_rollout_steer_segment(
    monkeypatch, tmp_path,
):
    """A fresh browser must receive tools omitted by itemsView=full.

    Three visible prompts can share one native Codex turn after steering.  The
    rollout fallback must use the official locator's segment index rather than
    taking the first row with the shared forkPointId.
    """
    sid = "fresh-browser-steered-detail"
    native_turn_id = "native-task-with-three-segments"
    rollout = tmp_path / "fresh-browser-steered-detail.jsonl"
    rollout.write_text(
        '{"type":"session_meta","payload":{"id":"fresh-browser-steered-detail"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(rollout))
    official_rows = (
        UserMsg(
            sid=sid, msg_id="official-user-second", prompt="second steer",
        ).model_dump(mode="json"),
        AssistantMsgStart(
            sid=sid, message_id="official-final", channel="final",
        ).model_dump(mode="json"),
        Delta(
            sid=sid, message_id="official-final", channel="final",
            text="done",
        ).model_dump(mode="json"),
        AssistantMsgEnd(
            sid=sid, message_id="official-final", channel="final",
        ).model_dump(mode="json"),
        TurnEnd(
            sid=sid, turn_id=native_turn_id,
            result=TurnResult(
                subtype="success", duration_ms=1, is_error=False),
        ).model_dump(mode="json"),
    )

    class OfficialHistory:
        async def turn_events(self, _sid, _turn_id):
            return official_rows

        def turn_detail_source(self, _sid, _turn_id):
            return "full"

        def rollout_fallback(self, _sid, _turn_id):
            return SimpleNamespace(
                before=None,
                limit=4,
                native_turn_id=native_turn_id,
                segment_index=1,
                segment_count=3,
            )

    selected_ids = []

    class DetailIndex:
        def get_turn_detail(
            self, _sid, _engine, _source, turn_id,
        ):
            selected_ids.append(turn_id)
            return (
                UserMsg(
                    sid=sid, msg_id=turn_id, prompt="second steer",
                ).model_dump(mode="json"),
                ProcessEvent(
                    sid=sid,
                    item_id="second-segment-command",
                    kind="command",
                    phase="end",
                    status="succeeded",
                    title="运行命令",
                ).model_dump(mode="json"),
                TurnEnd(
                    sid=sid, turn_id=native_turn_id,
                    result=TurnResult(
                        subtype="success", duration_ms=1, is_error=False),
                ).model_dump(mode="json"),
            )

    async def run():
        machine, _transport = _mk_machine()
        machine._codex_history = OfficialHistory()
        machine._history_index = DetailIndex()
        ctx = _mk_ctx(sid, sid)
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx

        async def build_history(_sid, **_kwargs):
            return History(
                session_id=sid,
                revision=machine._history_revision(sid),
                detail="summary",
                turns=[
                    {
                        "id": "rollout-user-first",
                        "prompt": "first",
                        "blocks": [],
                        "done": True,
                        "forkPointId": native_turn_id,
                        "detailEventCount": 1,
                        "detailLoaded": False,
                    },
                    {
                        "id": "rollout-user-second",
                        "prompt": "second steer",
                        "blocks": [],
                        "done": True,
                        "forkPointId": native_turn_id,
                        "detailEventCount": 1,
                        "detailLoaded": False,
                    },
                    {
                        "id": "rollout-user-third",
                        "prompt": "third steer",
                        "blocks": [],
                        "done": True,
                        "forkPointId": native_turn_id,
                        "detailEventCount": 1,
                        "detailLoaded": False,
                    },
                ],
            )

        async def no_image_supplement(_sid, _turn_id, rows, **_kwargs):
            return list(rows)

        machine._build_history = build_history
        machine._supplement_codex_history_image_views = no_image_supplement
        detail = await machine._handle_get_turn_detail(SimpleNamespace(
            session_id=sid,
            turn_id="official-user-second",
            client_id="fresh-browser",
            revision=machine._history_revision(sid),
            before=None,
            limit=192,
        ))

        assert selected_ids == ["rollout-user-second"]
        assert any(
            event.get("item_id") == "second-segment-command"
            for event in detail.events
        )
        assert not any(
            event.get("message_id") == "official-final"
            for event in detail.events
        )
        projected = materialize_history_turns(detail.events)
        assert len(projected) == 1
        assert projected[0]["id"] == "official-user-second"
        assert projected[0]["prompt"] == "second steer"

    asyncio.run(run())


@pytest.mark.parametrize(
    "local_detail_state",
    ["missing-entry", "missing-index", "missing-rollout"],
)
def test_codex_full_turn_detail_uses_official_rows_when_local_detail_misses(
    monkeypatch, tmp_path, local_detail_state,
):
    sid = "official-detail-index-miss"
    native_turn_id = "native-index-miss"
    rollout = tmp_path / "official-detail-index-miss.jsonl"
    rollout.write_text(
        '{"type":"session_meta","payload":{"id":"official-detail-index-miss"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        mm,
        "codex_rollout_path",
        lambda _sid: (
            None if local_detail_state == "missing-rollout" else str(rollout)
        ),
    )
    official_rows = (
        UserMsg(
            sid=sid, msg_id="official-user", prompt="inspect",
        ).model_dump(mode="json"),
        AssistantMsgStart(
            sid=sid, message_id="official-final", channel="final",
        ).model_dump(mode="json"),
        Delta(
            sid=sid, message_id="official-final", channel="final", text="done",
        ).model_dump(mode="json"),
        AssistantMsgEnd(
            sid=sid, message_id="official-final", channel="final",
        ).model_dump(mode="json"),
        TurnEnd(
            sid=sid, turn_id=native_turn_id,
            result=TurnResult(
                subtype="success", duration_ms=1, is_error=False),
        ).model_dump(mode="json"),
    )

    class OfficialHistory:
        async def turn_events(self, _sid, _turn_id):
            return official_rows

        def turn_detail_source(self, _sid, _turn_id):
            return "full"

        def rollout_fallback(self, _sid, _turn_id):
            return SimpleNamespace(
                before=None,
                limit=4,
                native_turn_id=native_turn_id,
                segment_index=0,
                segment_count=1,
            )

    class MissingDetailIndex:
        def get_turn_detail(self, *_args):
            return None

    async def run():
        machine, _transport = _mk_machine()
        machine._codex_history = OfficialHistory()
        machine._history_index = (
            None
            if local_detail_state == "missing-index"
            else MissingDetailIndex()
        )
        ctx = _mk_ctx(sid, sid)
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx

        async def build_history(_sid, **_kwargs):
            return History(
                session_id=sid,
                revision=machine._history_revision(sid),
                detail="summary",
                turns=[{
                    "id": "rollout-user",
                    "prompt": "inspect",
                    "blocks": [],
                    "done": True,
                    "forkPointId": native_turn_id,
                    "detailEventCount": 5,
                    "detailLoaded": False,
                }],
            )

        async def no_image_supplement(_sid, _turn_id, rows, **_kwargs):
            return list(rows)

        machine._build_history = build_history
        machine._supplement_codex_history_image_views = no_image_supplement
        detail = await machine._handle_get_turn_detail(SimpleNamespace(
            session_id=sid,
            turn_id="official-user",
            client_id="fresh-browser",
            revision=machine._history_revision(sid),
            before=None,
            limit=192,
        ))

        assert detail.authoritative is True
        assert detail.error is None
        assert any(
            event.get("message_id") == "official-final"
            for event in detail.events
        )
        assert materialize_history_turns(detail.events)[0]["id"] == (
            "official-user"
        )

    asyncio.run(run())


def test_codex_turn_detail_identity_rebind_rejects_another_segment():
    rows = (
        UserMsg(
            sid="identity-guard",
            msg_id="rollout-user-other",
            prompt="another steer",
        ).model_dump(mode="json"),
        TurnEnd(
            sid="identity-guard",
            turn_id="native-shared-turn",
            result=TurnResult(
                subtype="success", duration_ms=1, is_error=False),
        ).model_dump(mode="json"),
    )

    try:
        mm._rebind_turn_detail_visible_id(
            rows,
            indexed_turn_id="rollout-user-target",
            visible_turn_id="official-user-target",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("an unrelated rollout segment was rebound")

    assert rows[0]["msg_id"] == "rollout-user-other"


def test_protocol_v21_get_history_and_materialized_summary_roundtrip():
    gh = GetHistory(
        session_id="s1", client_id="c1", limit=50, detail="summary")
    assert deserialize(serialize(gh)) == gh
    h = History(session_id="s1", revision="test-revision",
                generation="test-generation", build_seq=4, live_seq=11,
                events=[{"type": "user_msg", "msg_id": "u1"}],
                turns=[{
                    "id": "u1", "prompt": "hello", "blocks": [],
                    "imageRefs": [{
                        "image_id": "u1.img.0", "media_type": "image/png",
                        "width": 1, "height": 1, "byte_size": 24,
                    }],
                    "done": True, "detailEventCount": 3,
                    "detailLoaded": False,
                }],
                detail="summary",
                has_more=True, oldest_id="u1", newest_id="u9",
                in_progress=True)
    got = deserialize(serialize(h))
    assert got.type == "history" and got.session_id == "s1" and got.has_more is True
    assert got.events[0]["type"] == "user_msg"
    assert got.turns[0].id == "u1" and got.turns[0].detailEventCount == 3
    assert got.turns[0].imageRefs[0]["image_id"] == "u1.img.0"
    assert got.detail == "summary"
    assert got.in_progress is True
    assert got.build_seq == 4 and got.live_seq == 11
    assert got.generation == "test-generation"
    assert got.authoritative is True and got.error is None

    marker = HistoryInvalidated(
        session_id="s1", revision="test-revision-2", reason="rollback"
    )
    assert deserialize(serialize(marker)) == marker
    assert is_downstream(marker) is True

    request = GetTurnDetail(
        session_id="s1", turn_id="u1", client_id="c1",
        revision="test-revision",
    )
    assert deserialize(serialize(request)) == request

    image_request = GetHistoryImage(
        session_id="s1", turn_id="u1", image_id="u1.img.0",
        variant="thumbnail", request_id="request-1", client_id="c1",
        revision="test-revision",
    )
    assert deserialize(serialize(image_request)) == image_request
    image = HistoryImage(
        session_id="s1", turn_id="u1", image_id="u1.img.0",
        variant="thumbnail", request_id="request-1",
        revision="test-revision", media_type="image/png",
        width=1, height=1, data="aW1n", to="c1",
    )
    assert deserialize(serialize(image)) == image


def test_turn_detail_cursor_pages_keep_display_groups_complete():
    rows = [
        {"type": "user_msg", "msg_id": "turn-1", "prompt": "inspect"},
        {"type": "assistant_msg_start", "message_id": "message-1"},
        {"type": "delta", "message_id": "message-1", "text": "first"},
        {"type": "assistant_msg_end", "message_id": "message-1",
         "text": "first"},
        {"type": "tool_use", "tool_use_id": "tool-1", "tool": "Read",
         "input": {}},
        {"type": "tool_delta", "tool_use_id": "tool-1",
         "stream": "output", "delta": "reading"},
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "done"},
        {"type": "process", "item_id": "process-1", "phase": "start"},
        {"type": "process", "item_id": "process-1", "phase": "update"},
        {"type": "process", "item_id": "process-1", "phase": "end"},
        {"type": "assistant_msg_start", "message_id": "message-2"},
        {"type": "delta", "message_id": "message-2", "text": "second"},
        {"type": "assistant_msg_end", "message_id": "message-2",
         "text": "second"},
        {"type": "turn_end", "turn_id": "turn-1", "result": {
            "subtype": "success", "duration_ms": 1, "is_error": False,
        }},
    ]

    newest, has_more, oldest, has_newer, newer = mm._turn_detail_page(
        rows, before=None, limit=2)
    assert [row["type"] for row in newest] == [
        "user_msg",
        "process",
        "assistant_msg_start", "delta", "assistant_msg_end",
        "turn_end",
    ]
    assert {row.get("item_id") for row in newest
            if row["type"] == "process"} == {"process-1"}
    assert {row.get("message_id") for row in newest
            if row["type"] in {
                "assistant_msg_start", "delta", "assistant_msg_end",
            }} == {"message-2"}
    assert (has_more, oldest, has_newer, newer) == (
        True, "2", False, None)

    older, has_more, oldest, has_newer, newer = mm._turn_detail_page(
        rows, before="2", limit=2)
    assert [row["type"] for row in older] == [
        "user_msg",
        "assistant_msg_start", "delta", "assistant_msg_end",
        "tool_use", "tool_delta", "tool_result",
        "turn_end",
    ]
    assert {row.get("message_id") for row in older
            if row["type"] in {
                "assistant_msg_start", "delta", "assistant_msg_end",
            }} == {"message-1"}
    assert {row.get("tool_use_id") for row in older
            if row["type"] in {
                "tool_use", "tool_delta", "tool_result",
            }} == {"tool-1"}
    assert (has_more, oldest, has_newer, newer) == (
        False, None, True, "4")

    roundtrip, *_ = mm._turn_detail_page(
        rows, before=newer, limit=2)
    assert roundtrip == newest


def test_turn_detail_byte_bounded_cursors_visit_adjacent_pages():
    rows = [
        {"type": "user_msg", "msg_id": "turn-1", "prompt": "inspect"},
        *[
            {
                "type": "process",
                "item_id": f"process-{index}",
                "phase": "update",
                "text": "x" * 300,
            }
            for index in range(8)
        ],
        {"type": "turn_end", "turn_id": "turn-1", "result": {
            "subtype": "success", "duration_ms": 1, "is_error": False,
        }},
    ]

    newest, has_more, older_cursor, has_newer, newer_cursor = (
        mm._turn_detail_page(
            rows, before=None, limit=4, max_bytes=750)
    )
    assert has_more is True
    assert has_newer is False
    assert newer_cursor is None

    middle, has_more, next_older, has_newer, back_to_newest = (
        mm._turn_detail_page(
            rows, before=older_cursor, limit=4, max_bytes=750)
    )
    assert has_more is True
    assert has_newer is True
    assert next_older is not None
    assert back_to_newest is not None

    older, _, _, has_newer, back_to_middle = mm._turn_detail_page(
        rows, before=next_older, limit=4, max_bytes=750)
    assert has_newer is True
    assert back_to_middle is not None
    assert mm._turn_detail_page(
        rows, before=back_to_middle, limit=4, max_bytes=750)[0] == middle
    assert mm._turn_detail_page(
        rows, before=back_to_newest, limit=4, max_bytes=750)[0] == newest

    visible_ids = {
        row["item_id"]
        for page in (newest, middle, older)
        for row in page
        if row["type"] == "process"
    }
    assert visible_ids
    assert len(visible_ids) == sum(
        row["type"] == "process"
        for page in (newest, middle, older)
        for row in page
    )


def test_turn_detail_page_keeps_a_legal_large_final_message_exact():
    final_text = "x" * (5 * 1024 * 1024)
    rows = [
        {"type": "user_msg", "msg_id": "turn-1", "prompt": "finish"},
        {"type": "assistant_msg_start", "message_id": "final-1",
         "channel": "final"},
        {"type": "delta", "message_id": "final-1", "channel": "final",
         "text": final_text},
        {"type": "assistant_msg_end", "message_id": "final-1",
         "channel": "final"},
        {"type": "turn_end", "turn_id": "turn-1", "result": {
            "subtype": "success", "duration_ms": 1, "is_error": False,
        }},
    ]

    page, has_more, oldest, has_newer, newer = mm._turn_detail_page(
        rows,
        before=None,
        limit=192,
        max_bytes=8 * 1024 * 1024,
    )

    assert (has_more, oldest, has_newer, newer) == (
        False, None, False, None)
    assert next(row for row in page if row["type"] == "delta")["text"] == final_text


def test_materialized_summary_keeps_image_metadata_without_full_payload():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + (
        b"\x00\x00\x00\x02\x00\x00\x00\x03")
    encoded = base64.b64encode(png).decode("ascii")
    events = [
        {"type": "user_msg", "msg_id": "turn-image", "prompt": "look",
         "images": [{"media_type": "image/png", "data": encoded}]},
        {"type": "turn_end", "turn_id": "turn-image",
         "result": {"subtype": "success", "duration_ms": 1,
                    "is_error": False}},
    ]
    turns = materialize_history_turns(events)
    assert len(turns) == 1
    assert "images" not in turns[0]
    assert turns[0]["imageRefs"] == [{
        "image_id": history_image_id("turn-image", 0),
        "media_type": "image/png",
        "width": 2,
        "height": 3,
        "byte_size": len(png),
    }]
    assert encoded not in json.dumps(turns)


def test_materialized_summary_never_exposes_bare_error_sentinel():
    turns = materialize_history_turns((
        {"type": "user_msg", "msg_id": "message-1", "prompt": "go"},
        {"type": "turn_end", "turn_id": "turn-1", "result": {
            "subtype": "error", "is_error": True, "duration_ms": 0,
        }},
    ))
    assert turns[0]["error"] == "该轮未正常结束"


def test_materialized_summary_never_exposes_untrusted_error_text():
    turns = materialize_history_turns((
        {"type": "user_msg", "msg_id": "message-private", "prompt": "go"},
        {"type": "error", "message":
         "provider crash at /private/token; Authorization: Bearer secret"},
        {"type": "turn_end", "turn_id": "turn-private", "result": {
            "subtype": "error", "is_error": True, "duration_ms": 0,
        }},
    ))

    assert turns[0]["error"] == "该轮未正常结束"
    assert "/private/token" not in json.dumps(turns, ensure_ascii=False)

    safe = materialize_history_turns((
        {"type": "user_msg", "msg_id": "message-network", "prompt": "go"},
        {"type": "error", "message": "网络连接异常，请检查网络后重试。"},
        {"type": "turn_end", "turn_id": "turn-network", "result": {
            "subtype": "error", "is_error": True, "duration_ms": 0,
        }},
    ))
    assert safe[0]["error"] == "网络连接异常，请检查网络后重试。"
    detail = TurnDetail(
        session_id="s1", turn_id="u1", revision="test-revision",
        events=[{"type": "user_msg", "msg_id": "u1", "prompt": "hello"}],
    )
    assert deserialize(serialize(detail)) == detail


def test_history_revision_is_boot_scoped_and_monotonic():
    first, _ = _mk_machine()
    restarted, _ = _mk_machine()

    initial = first._history_revision("s1")
    assert restarted._history_revision("s1") != initial
    assert first._bump_history_revision("s1") != initial
    assert first._history_revision("s1").endswith("-1")


def test_history_read_does_not_block_serial_commands_or_duplicate_retries():
    """A slow transcript read must not hold query/interrupt command intake."""
    async def go():
        machine, _ = _mk_machine()
        history_started = asyncio.Event()
        release_history = asyncio.Event()
        query_seen = asyncio.Event()
        history_calls = 0

        async def process(command):
            nonlocal history_calls
            if command.type == "get_history":
                history_calls += 1
                history_started.set()
                await release_history.wait()
                return
            if command.type == "query":
                query_seen.set()

        machine._process_command = process
        history = SimpleNamespace(
            type="get_history", client_id="client-1", cmd_id="history-1")
        machine._start_history_command(history)
        await asyncio.wait_for(history_started.wait(), timeout=1)

        # A reconnect retry shares the in-flight read instead of scanning the
        # same rollout/transcript twice.
        machine._start_history_command(history)
        await machine._process_command_safely(SimpleNamespace(type="query"))
        assert query_seen.is_set()
        assert history_calls == 1

        release_history.set()
        await asyncio.gather(*machine._history_command_tasks.values())

    asyncio.run(go())


def test_distinct_history_commands_share_one_page_build_and_route_per_client():
    async def go():
        machine, transport = _mk_machine()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def build(sid, *, before, limit, cwd, detail):
            nonlocal calls
            calls += 1
            assert (sid, before, limit, cwd) == ("session-1", None, 4, None)
            assert detail == "full"
            started.set()
            await release.wait()
            return History(
                session_id=sid,
                revision="revision-1",
                events=[{"type": "user_msg", "msg_id": "turn-1"}],
                has_more=False,
            )

        machine._build_requested_history = build
        first = SimpleNamespace(
            session_id="session-1", client_id="client-1",
            cmd_id="command-1", before=None, limit=4, cwd=None,
        )
        second = SimpleNamespace(
            session_id="session-1", client_id="client-2",
            cmd_id="command-2", before=None, limit=4, cwd=None,
        )
        tasks = [
            asyncio.create_task(machine._handle_get_history(first)),
            asyncio.create_task(machine._handle_get_history(second)),
        ]
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        await asyncio.gather(*tasks)

        histories = [message for message in transport.sent
                     if isinstance(message, History)]
        assert {message.to for message in histories} == {"client-1", "client-2"}
        assert all(message.events == [
            {"type": "user_msg", "msg_id": "turn-1"}
        ] for message in histories)
        assert calls == 1

    asyncio.run(go())


def test_history_content_does_not_wait_for_external_ownership_scan():
    async def go():
        machine, _ = _mk_machine()
        machine._watch_session = lambda sid: machine._watch.setdefault(
            sid, {"engine": "codex"})
        prime_called = False

        async def blocked_prime(_sid):
            nonlocal prime_called
            prime_called = True
            await asyncio.Event().wait()

        async def build(sid, **kwargs):
            return History(
                session_id=sid,
                revision="revision-1",
                detail=kwargs["detail"],
            )

        machine._prime_codex_ownership = blocked_prime
        machine._build_history = build
        machine._codex_history = SimpleNamespace(
            summary_page=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                CodexHistoryUnsupported("compatibility path"))
        )
        history = await asyncio.wait_for(machine._build_requested_history(
            "session-1", before=None, limit=4, cwd=None, detail="summary",
        ), timeout=0.1)
        assert history.session_id == "session-1"
        assert prime_called is False

    asyncio.run(go())


def test_get_turn_detail_is_routed_and_revision_bound(monkeypatch, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(rollout))
    events = (
        {"type": "user_msg", "sid": "session-1", "msg_id": "message-1",
         "prompt": "inspect"},
        {"type": "tool_use", "sid": "session-1", "tool_use_id": "tool-1",
         "tool": "Read", "input": {"file_path": "/tmp/example"}},
        {"type": "tool_result", "sid": "session-1", "tool_use_id": "tool-1",
         "content": "ok", "is_error": False},
        {"type": "turn_end", "sid": "session-1", "turn_id": "turn-1",
         "result": {"subtype": "success", "duration_ms": 1,
                    "is_error": False}},
    )

    async def go():
        machine, transport = _mk_machine()
        machine._history_index = HistoryIndexStore(tmp_path / "state")
        ctx = _mk_ctx("session-1", "session-1")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        source = HistorySourceFingerprint.capture(rollout)
        page = MaterializedHistoryPage(
            events=events, has_more=False,
            oldest_id="message-1", newest_id="message-1",
            turns=materialize_history_turns(events),
        )
        machine._history_index.put_page(
            "session-1", "codex", source, before=None, limit=4, page=page)
        revision = machine._history_revision("session-1")

        response = await machine._handle_get_turn_detail(SimpleNamespace(
            session_id="session-1", turn_id="message-1",
            client_id="client-1", revision=revision,
        ))
        assert isinstance(response, TurnDetail)
        assert response.to == "client-1" and response.sid == "session-1"
        assert response.authoritative is True
        assert response.events == list(events)

        stale = await machine._handle_get_turn_detail(SimpleNamespace(
            session_id="session-1", turn_id="message-1",
            client_id="client-2", revision="old-revision",
        ))
        assert stale.to == "client-2"
        assert stale.authoritative is False and stale.events == []

        assert transport.sent[-2:] == [response, stale]

    asyncio.run(go())


def test_running_claude_turn_detail_materializes_current_source_on_cache_miss(
    monkeypatch, tmp_path,
):
    transcript = tmp_path / "session-running.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setattr(mm, "transcript_path", lambda _sid: str(transcript))
    events = (
        {"type": "user_msg", "sid": "session-running",
         "msg_id": "message-running", "prompt": "work"},
        {"type": "process", "sid": "session-running",
         "item_id": "process-running", "kind": "command",
         "phase": "end", "status": "succeeded", "title": "运行命令"},
    )

    async def go():
        machine, _transport = _mk_machine()
        machine._history_index = HistoryIndexStore(tmp_path / "state-running")
        ctx = _mk_ctx("session-running", "session-running")
        ctx.engine = "claude"
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx
        builds = []

        async def build_history(sid, **kwargs):
            builds.append((sid, kwargs))
            source = HistorySourceFingerprint.capture(transcript)
            machine._history_index.put_turn_details(
                sid, "claude", source, events)
            return History(
                session_id=sid,
                revision=machine._history_revision(sid),
                detail=kwargs["detail"],
            )

        machine._build_history = build_history
        response = await machine._handle_get_turn_detail(SimpleNamespace(
            session_id="session-running",
            turn_id="message-running",
            client_id="client-running",
            revision=machine._history_revision("session-running"),
            before=None,
            limit=192,
        ))

        assert builds == [("session-running", {
            "before": None,
            "limit": 4,
            "cwd_hint": ctx.cwd,
            "detail": "summary",
            "allow_stale": False,
        })]
        assert response.authoritative is True
        assert response.error is None
        assert any(
            row.get("item_id") == "process-running"
            for row in response.events
        )

    asyncio.run(go())


def test_get_history_image_is_revision_bound_lazy_and_cached(
    monkeypatch, tmp_path,
):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (640, 320), (26, 84, 140)).save(buffer, "PNG")
    raw = buffer.getvalue()
    encoded = base64.b64encode(raw).decode("ascii")
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(rollout))
    events = (
        {"type": "user_msg", "sid": "session-1", "msg_id": "message-1",
         "prompt": "inspect", "images": [{
             "media_type": "image/png", "data": encoded,
         }]},
        {"type": "turn_end", "sid": "session-1", "turn_id": "turn-1",
         "result": {"subtype": "success", "duration_ms": 1,
                    "is_error": False}},
    )

    async def go():
        machine, transport = _mk_machine()
        machine._history_index = HistoryIndexStore(tmp_path / "state")
        ctx = _mk_ctx("session-1", "session-1")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        source = HistorySourceFingerprint.capture(rollout)
        page = MaterializedHistoryPage(
            events=events, has_more=False,
            oldest_id="message-1", newest_id="message-1",
            turns=materialize_history_turns(events),
        )
        machine._history_index.put_page(
            "session-1", "codex", source, before=None, limit=4, page=page)
        revision = machine._history_revision("session-1")
        image_id = history_image_id("message-1", 0)

        thumbnail = await machine._handle_get_history_image(SimpleNamespace(
            session_id="session-1", turn_id="message-1", image_id=image_id,
            variant="thumbnail", request_id="request-thumb",
            client_id="client-1", revision=revision,
        ))
        assert isinstance(thumbnail, HistoryImage)
        assert thumbnail.to == "client-1" and thumbnail.error is None
        assert thumbnail.media_type == "image/webp"
        assert thumbnail.width == 360 and thumbnail.height == 180
        assert len(base64.b64decode(thumbnail.data)) < len(raw)

        full = await machine._handle_get_history_image(SimpleNamespace(
            session_id="session-1", turn_id="message-1", image_id=image_id,
            variant="full", request_id="request-full",
            client_id="client-1", revision=revision,
        ))
        assert full.error is None and full.media_type == "image/png"
        assert full.width == 640 and full.height == 320
        assert base64.b64decode(full.data) == raw

        # A second request is served by the source-bound bounded image cache;
        # decoding/thumbnailing must not run again.
        monkeypatch.setattr(
            mm, "_render_history_image",
            lambda *_args: (_ for _ in ()).throw(AssertionError("re-rendered")),
        )
        cached = await machine._handle_get_history_image(SimpleNamespace(
            session_id="session-1", turn_id="message-1", image_id=image_id,
            variant="thumbnail", request_id="request-cached",
            client_id="client-1", revision=revision,
        ))
        assert cached.error is None and cached.data == thumbnail.data

        stale = await machine._handle_get_history_image(SimpleNamespace(
            session_id="session-1", turn_id="message-1", image_id=image_id,
            variant="full", request_id="request-stale",
            client_id="client-2", revision="old-revision",
        ))
        assert stale.to == "client-2" and stale.data is None
        assert stale.error == "会话历史已更新，请重新加载图片"
        assert transport.sent[-4:] == [thumbnail, full, cached, stale]

    asyncio.run(go())


def test_history_image_tool_asset_is_served_only_from_current_turn_reference(
    monkeypatch, tmp_path,
):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (320, 160), (34, 120, 88)).save(buffer, "PNG")
    raw = buffer.getvalue()
    rollout = tmp_path / "rollout-tool-image.jsonl"
    rollout.write_text("{}\n")
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(rollout))
    image_id = "img-tool-view-1"
    events = (
        {"type": "user_msg", "sid": "session-1", "msg_id": "message-1",
         "prompt": "inspect"},
        ProcessEvent(
            item_id="view-1",
            kind="server_tool",
            phase="end",
            status="succeeded",
            turn_id="native-1",
            title="查看图片",
            tool="view_image",
            input={
                "file_path": "/tmp/chart.png",
                "history_image": {
                    "image_id": image_id,
                    "media_type": "image/png",
                    "width": 320,
                    "height": 160,
                    "byte_size": len(raw),
                },
            },
        ).model_dump(mode="json"),
        {"type": "turn_end", "sid": "session-1", "turn_id": "native-1",
         "result": {"subtype": "success", "duration_ms": 1,
                    "is_error": False}},
    )

    async def go():
        machine, _ = _mk_machine()
        machine._history_index = HistoryIndexStore(tmp_path / "state-tool-image")
        ctx = _mk_ctx("session-1", "session-1")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        source = HistorySourceFingerprint.capture(rollout)
        page = MaterializedHistoryPage(
            events=events,
            has_more=False,
            oldest_id="message-1",
            newest_id="message-1",
            turns=materialize_history_turns(events),
        )
        machine._history_index.put_page(
            "session-1", "codex", source, before=None, limit=4, page=page)
        machine._history_index.put_image_asset(
            "session-1", "codex", source, "message-1", image_id, "full",
            "image/png", 320, 160, raw,
        )
        revision = machine._history_revision("session-1")

        thumbnail = await machine._handle_get_history_image(SimpleNamespace(
            session_id="session-1",
            turn_id="message-1",
            image_id=image_id,
            variant="thumbnail",
            request_id="request-tool-image",
            client_id="client-1",
            revision=revision,
        ))
        assert thumbnail.error is None
        assert thumbnail.media_type == "image/webp"
        assert thumbnail.width == 320 and thumbnail.height == 160

        missing = await machine._handle_get_history_image(SimpleNamespace(
            session_id="session-1",
            turn_id="message-1",
            image_id="img-guessed",
            variant="full",
            request_id="request-guessed",
            client_id="client-1",
            revision=revision,
        ))
        assert missing.data is None
        assert missing.error == "未找到这张历史图片"

    asyncio.run(go())


def test_codex_summary_keeps_rollout_image_refs_and_lazy_source_detail(
    monkeypatch, tmp_path,
):
    from PIL import Image

    encoded_images = []
    raw_images = []
    for size, color in (
        ((80, 40), (26, 84, 140)),
        ((32, 64), (194, 65, 12)),
    ):
        buffer = io.BytesIO()
        Image.new("RGB", size, color).save(buffer, "PNG")
        raw = buffer.getvalue()
        raw_images.append(raw)
        encoded_images.append(base64.b64encode(raw).decode("ascii"))

    rollout = tmp_path / "rollout-images.jsonl"
    rollout.write_text("".join(json.dumps(row) + "\n" for row in [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "session-images"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-images"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "response_item",
         "payload": {
             "type": "message", "role": "user", "content": [
                 {"type": "input_text", "text": "inspect both"},
                 *[
                     {"type": "input_image",
                      "image_url": f"data:image/png;base64,{encoded}"}
                     for encoded in encoded_images
                 ],
             ],
         }},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "inspect both"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-images",
                     "last_agent_message": "done"}},
    ]))
    monkeypatch.setattr(
        mm, "codex_rollout_path", lambda _sid: str(rollout))

    async def go():
        machine, _ = _mk_machine()
        machine._history_index = HistoryIndexStore(tmp_path / "state-images")
        ctx = _mk_ctx("session-images", "session-images")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx

        summary = await machine._build_history(
            "session-images", limit=4, detail="summary")
        assert len(summary.turns) == 1
        refs = summary.turns[0].imageRefs
        assert refs is not None and len(refs) == 2
        assert [(ref["width"], ref["height"]) for ref in refs] == [
            (80, 40), (32, 64)]
        # Summary/history cache frames never retain the base64 image bodies.
        assert all(row.get("images") is None for row in summary.events)
        source = HistorySourceFingerprint.capture(rollout)
        indexed = machine._history_index.get_page(
            "session-images", "codex", source, before=None, limit=4)
        assert indexed is not None
        assert indexed.turns[0]["imageRefs"] == refs
        assert all(row.get("images") is None for row in indexed.events)

        # The independent source-complete detail row remains available to the
        # lazy image endpoint even though cache/wire summary rows have no body.
        revision = machine._history_revision("session-images")
        for index, ref in enumerate(refs):
            result = await machine._handle_get_history_image(SimpleNamespace(
                session_id="session-images",
                turn_id="turn-images",
                image_id=ref["image_id"],
                variant="full",
                request_id=f"request-{index}",
                client_id="client-1",
                revision=revision,
            ))
            assert result.error is None
            assert base64.b64decode(result.data) == raw_images[index]

    asyncio.run(go())


def test_history_build_materializes_source_bound_shadow_page(
    monkeypatch, tmp_path,
):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("".join(json.dumps(row) + "\n" for row in [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "session-1"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-1"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "hello"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-1",
                     "last_agent_message": "world"}},
    ]))
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(rollout))
    translate = mm.codex_translate_history
    translate_calls = 0

    def counted_translate(*args, **kwargs):
        nonlocal translate_calls
        translate_calls += 1
        return translate(*args, **kwargs)

    monkeypatch.setattr(mm, "codex_translate_history", counted_translate)

    async def go():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("session-1", "session-1")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx

        history = await machine._build_history("session-1", limit=4)
        source = HistorySourceFingerprint.capture(rollout)
        indexed = machine._history_index.get_page(
            "session-1", "codex", source, before=None, limit=4)
        assert indexed is not None
        assert list(indexed.events) == history.events
        assert indexed.oldest_id == history.oldest_id
        assert indexed.newest_id == history.newest_id

        # A second identical build must preserve exact shadow parity.
        repeated = await machine._build_history("session-1", limit=4)
        repeated_page = machine._history_index.get_page(
            "session-1", "codex", source, before=None, limit=4)
        assert repeated_page is not None
        assert repeated_page.semantically_equals(indexed)
        assert [row["type"] for row in repeated.events] == [
            row["type"] for row in history.events]
        assert translate_calls == 1

        # A destructive revision barrier invalidates the materialized page even
        # if a coarse filesystem timestamp happens not to change.
        machine._bump_history_revision("session-1")
        await machine._build_history("session-1", limit=4)
        assert translate_calls == 2

        # Ordinary append invalidation is source-fingerprint based.
        with rollout.open("a") as stream:
            stream.write(json.dumps({
                "timestamp": "2026-01-01T00:00:04Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-2"},
            }) + "\n")
        await machine._build_history("session-1", limit=4)
        assert translate_calls == 3

        summary = await machine._build_history(
            "session-1", limit=4, detail="summary")
        assert summary.detail == "summary"
        assert summary.turns
        assert all(row["type"] in {"model", "effort"}
                   for row in summary.events)
        assert len(summary.model_dump_json()) < len(history.model_dump_json())

    asyncio.run(go())


def test_running_codex_summary_keeps_bounded_live_projection(
    monkeypatch, tmp_path,
):
    rollout = tmp_path / "rollout-live-summary.jsonl"
    rollout.write_text("".join(json.dumps(row) + "\n" for row in [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "session-live"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-live"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "inspect"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "commentary",
                     "message": "working now"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "context_compacted"}},
    ]))
    monkeypatch.setattr(
        mm, "codex_rollout_path", lambda _sid: str(rollout))

    async def go():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("session-live", "session-live")
        ctx.engine = "codex"
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx

        history = await machine._build_history(
            "session-live", limit=4, detail="summary")

        assert history.detail == "summary"
        assert len(history.turns) == 1
        blocks = history.turns[0].blocks
        assert any(
            block.get("kind") == "text"
            and block.get("channel") == "commentary"
            and block.get("text") == "working now"
            for block in blocks
        )
        assert any(
            block.get("kind") == "process"
            and block.get("processKind") == "compaction"
            for block in blocks
        )
        assert all(row["type"] in {"model", "effort"}
                   for row in history.events)

    asyncio.run(go())


def test_history_index_reuses_only_verified_append_prefix(tmp_path):
    transcript = tmp_path / "claude.jsonl"
    transcript.write_bytes(b'{"type":"user","value":"old"}\n')
    store = HistoryIndexStore(tmp_path / "state-append")
    old_source = HistorySourceFingerprint.capture(transcript)
    page = MaterializedHistoryPage(
        events=({"type": "user_msg", "msg_id": "old", "prompt": "old"},),
        has_more=False,
        oldest_id="old",
        newest_id="old",
        turns=(),
    )
    store.put_page(
        "session-append", "claude", old_source,
        before=None, limit=4, page=page,
    )

    with transcript.open("ab") as stream:
        stream.write(b'{"type":"assistant","value":"new"}\n')
    appended = HistorySourceFingerprint.capture(transcript)
    reused = store.get_append_page(
        "session-append", "claude", appended, before=None, limit=4)
    assert reused is not None and reused.newest_id == "old"

    # Same path/inode and a larger size are insufficient: an in-place rewrite
    # is a destructive source change, not a safe stale-while-revalidate prefix.
    transcript.write_bytes(
        b'{"type":"user","value":"rewritten"}\n'
        b'{"type":"assistant","value":"more"}\n')
    rewritten = HistorySourceFingerprint.capture(transcript)
    assert store.get_append_page(
        "session-append", "claude", rewritten, before=None, limit=4,
    ) is None


def test_claude_history_append_paints_cached_page_before_revalidation(
        monkeypatch, tmp_path):
    transcript = tmp_path / "claude.jsonl"
    transcript.write_bytes(b'{"type":"user","value":"old"}\n')
    monkeypatch.setattr(mm, "transcript_path", lambda _sid: str(transcript))
    monkeypatch.setattr(
        mm,
        "get_session_messages",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("append-stale first paint performed a full scan")),
    )

    async def go():
        machine, _ = _mk_machine()
        machine._history_index = HistoryIndexStore(tmp_path / "state-fast")
        ctx = _mk_ctx("claude-fast", "claude-fast")
        ctx.engine = "claude"
        machine.sessions[ctx.key] = ctx
        old_source = HistorySourceFingerprint.capture(transcript)
        events = (
            {"type": "user_msg", "sid": "claude-fast",
             "msg_id": "old", "prompt": "old"},
            {"type": "turn_end", "sid": "claude-fast",
             "result": {"subtype": "success", "duration_ms": 1,
                        "is_error": False}},
        )
        page = MaterializedHistoryPage(
            events=events,
            has_more=False,
            oldest_id="old",
            newest_id="old",
            turns=materialize_history_turns(events),
        )
        machine._history_index.put_page(
            "claude-fast", "claude", old_source,
            before=None, limit=4, page=page,
        )
        with transcript.open("ab") as stream:
            stream.write(b'{"type":"assistant","value":"new"}\n')
        refreshes = []
        monkeypatch.setattr(
            machine,
            "_schedule_history_refresh",
            lambda sid, **kwargs: refreshes.append((sid, kwargs)),
        )

        history = await machine._build_requested_history(
            "claude-fast", before=None, limit=4, cwd=ctx.cwd,
            detail="summary",
        )

        assert [turn.prompt for turn in history.turns] == ["old"]
        assert history.authoritative is False
        assert refreshes and refreshes[0][0] == "claude-fast"

    asyncio.run(go())


def test_codex_history_append_paints_cached_page_before_revalidation(
        monkeypatch, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(
        b'{"type":"session_meta","payload":{"id":"codex-fast"}}\n')
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(rollout))
    monkeypatch.setattr(
        mm,
        "codex_translate_history",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError(
                "append-stale Codex first paint performed a full scan")),
    )

    async def go():
        machine, _ = _mk_machine()
        machine._history_index = HistoryIndexStore(tmp_path / "state-codex-fast")
        ctx = _mk_ctx("codex-fast", "codex-fast")
        ctx.engine = "codex"
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx
        old_source = HistorySourceFingerprint.capture(rollout)
        events = (
            {"type": "user_msg", "sid": "codex-fast",
             "msg_id": "old", "prompt": "old"},
            {"type": "turn_end", "sid": "codex-fast", "turn_id": "turn-old",
             "result": {"subtype": "success", "duration_ms": 1,
                        "is_error": False}},
        )
        page = MaterializedHistoryPage(
            events=events,
            has_more=True,
            oldest_id="old",
            newest_id="old",
            turns=materialize_history_turns(events),
        )
        machine._history_index.put_page(
            "codex-fast", "codex", old_source,
            before=None, limit=4, page=page,
        )
        with rollout.open("ab") as stream:
            stream.write(
                b'{"type":"event_msg","payload":{"type":"task_started"}}\n')
        refreshes = []
        monkeypatch.setattr(
            machine,
            "_schedule_history_refresh",
            lambda sid, **kwargs: refreshes.append((sid, kwargs)),
        )
        class Unsupported:
            async def summary_page(self, *_args, **_kwargs):
                raise CodexHistoryUnsupported("compatibility path")

        machine._codex_history = Unsupported()

        history = await machine._build_requested_history(
            "codex-fast", before=None, limit=4, cwd=ctx.cwd,
            detail="summary",
        )

        assert [turn.prompt for turn in history.turns] == ["old"]
        assert history.authoritative is False
        assert history.in_progress is True
        assert history.has_more is True
        assert refreshes and refreshes[0][0] == "codex-fast"

    asyncio.run(go())


def test_exact_history_cache_hit_that_grows_before_send_is_provisional(
        monkeypatch, tmp_path):
    rollout = tmp_path / "cache-race-rollout.jsonl"
    rollout.write_bytes(
        b'{"type":"session_meta","payload":{"id":"codex-cache-race"}}\n')
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(rollout))
    monkeypatch.setattr(
        mm,
        "codex_translate_history",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an exact cache race performed a full scan")),
    )

    async def go():
        machine, _ = _mk_machine()
        machine._history_index = HistoryIndexStore(
            tmp_path / "state-cache-race")
        ctx = _mk_ctx("codex-cache-race", "codex-cache-race")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        source = HistorySourceFingerprint.capture(rollout)
        events = (
            {"type": "user_msg", "sid": "codex-cache-race",
             "msg_id": "old", "prompt": "old"},
            {"type": "turn_end", "sid": "codex-cache-race",
             "turn_id": "turn-old",
             "result": {"subtype": "success", "duration_ms": 1,
                        "is_error": False}},
        )
        machine._history_index.put_page(
            "codex-cache-race",
            "codex",
            source,
            before=None,
            limit=4,
            page=MaterializedHistoryPage(
                events=events,
                has_more=False,
                oldest_id="old",
                newest_id="old",
                turns=materialize_history_turns(events),
            ),
        )
        original_get_page = machine._history_index.get_page
        appended = False

        def get_page(*args, **kwargs):
            nonlocal appended
            page = original_get_page(*args, **kwargs)
            if page is not None and not appended:
                appended = True
                with rollout.open("ab") as stream:
                    stream.write(
                        b'{"type":"event_msg",'
                        b'"payload":{"type":"task_started"}}\n')
            return page

        monkeypatch.setattr(machine._history_index, "get_page", get_page)
        refreshes = []
        monkeypatch.setattr(
            machine,
            "_schedule_history_refresh",
            lambda sid, **kwargs: refreshes.append((sid, kwargs)),
        )

        history = await machine._build_history(
            "codex-cache-race",
            limit=4,
            detail="summary",
            allow_stale=True,
        )

        assert history.authoritative is False
        assert [turn.prompt for turn in history.turns] == ["old"]
        assert refreshes and refreshes[0][0] == "codex-cache-race"

    asyncio.run(go())


def test_codex_history_growth_during_scan_is_provisional_without_index(
        monkeypatch, tmp_path):
    rollout = tmp_path / "growing-rollout.jsonl"
    rollout.write_bytes(
        b'{"type":"session_meta","payload":{"id":"codex-growing"}}\n')
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(rollout))
    monkeypatch.setattr(
        mm,
        "codex_history_window",
        lambda path, **_kwargs: (
            0, os.path.getsize(path), False, None, None,
        ),
    )

    translated = False

    def translate(*_args, **_kwargs):
        nonlocal translated
        if not translated:
            translated = True
            with rollout.open("ab") as stream:
                stream.write(
                    b'{"type":"event_msg","payload":{"type":"task_started"}}\n')
        return [
            UserMsg(msg_id="turn-1", prompt="hello"),
            TurnEnd(result=TurnResult(
                subtype="success", duration_ms=1, is_error=False)),
        ], None

    monkeypatch.setattr(mm, "codex_translate_history", translate)

    async def go():
        nonlocal translated
        machine, _ = _mk_machine()
        machine._history_index = None
        ctx = _mk_ctx("codex-growing", "codex-growing")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        refreshes = []
        monkeypatch.setattr(
            machine,
            "_schedule_history_refresh",
            lambda sid, **kwargs: refreshes.append((sid, kwargs)),
        )

        history = await machine._build_history(
            "codex-growing",
            limit=4,
            detail="summary",
            allow_stale=True,
        )

        assert history.authoritative is False
        assert history.error is None
        assert [turn.prompt for turn in history.turns] == ["hello"]
        assert refreshes == [(
            "codex-growing",
            {
                "before": None,
                "limit": 4,
                "cwd": None,
                "detail": "summary",
            },
        )]

        refreshes.clear()
        translated = False
        older = await machine._build_history(
            "codex-growing",
            before="turn-1",
            limit=4,
            detail="summary",
            allow_stale=True,
        )
        assert older.authoritative is False
        assert refreshes == []

    asyncio.run(go())


def test_history_index_write_failure_keeps_coherent_source_authoritative(
        monkeypatch, tmp_path):
    rollout = tmp_path / "stable-rollout.jsonl"
    rollout.write_bytes(
        b'{"type":"session_meta","payload":{"id":"codex-stable"}}\n')
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(rollout))
    monkeypatch.setattr(
        mm,
        "codex_history_window",
        lambda path, **_kwargs: (
            0, os.path.getsize(path), False, None, None,
        ),
    )
    monkeypatch.setattr(
        mm,
        "codex_translate_history",
        lambda *_args, **_kwargs: ([
            UserMsg(msg_id="turn-1", prompt="hello"),
            TurnEnd(result=TurnResult(
                subtype="success", duration_ms=1, is_error=False)),
        ], None),
    )

    async def go():
        machine, _ = _mk_machine()
        machine._history_index = HistoryIndexStore(tmp_path / "state-write-fail")
        ctx = _mk_ctx("codex-stable", "codex-stable")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        monkeypatch.setattr(
            machine._history_index,
            "put_page",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("disk full")),
        )

        history = await machine._build_history(
            "codex-stable",
            limit=4,
            detail="summary",
            allow_stale=True,
        )

        assert history.authoritative is True
        assert history.error is None
        assert [turn.prompt for turn in history.turns] == ["hello"]

    asyncio.run(go())


def test_nonresident_claude_history_uses_authoritative_session_cwd(
        monkeypatch, tmp_path):
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text("{}\n")
    calls = []
    canned = [
        UserMsg(msg_id="turn-1", prompt="hello"),
        TurnEnd(result=TurnResult(
            subtype="success", duration_ms=1, is_error=False)),
    ]
    monkeypatch.setattr(mm, "transcript_path", lambda _sid: str(transcript))
    monkeypatch.setattr(
        mm, "get_session_info",
        lambda _sid: SimpleNamespace(cwd="/authoritative/project"),
    )

    def messages(_sid, directory=None):
        calls.append(directory)
        return ["message"] if directory == "/authoritative/project" else []

    monkeypatch.setattr(mm, "get_session_messages", messages)
    monkeypatch.setattr(mm, "transcript_timestamps", lambda _sid: {})
    monkeypatch.setattr(
        mm, "transcript_internal_user_events", lambda _sid: [])
    monkeypatch.setattr(
        mm, "translate_history",
        lambda *_args, **_kwargs: [event.model_copy() for event in canned])
    monkeypatch.setattr(mm, "translate_subagent_history", lambda *_args: [])
    monkeypatch.setattr(mm, "last_assistant_model", lambda _msgs: None)

    async def go():
        machine, _ = _mk_machine()
        machine.cfg.cc_cwd = "/wrong/default"
        history = await machine._build_history(
            "claude-session", limit=4, cwd_hint="/stale/browser")

        assert [turn.prompt for turn in history.turns] == []
        assert [row["prompt"] for row in history.events
                if row["type"] == "user_msg"] == ["hello"]
        assert calls == ["/authoritative/project"]

    asyncio.run(go())


def test_compacted_claude_main_chain_recovers_precompact_history(
        monkeypatch, tmp_path):
    transcript = tmp_path / "compacted-claude.jsonl"
    rows = [
        {
            "type": "user", "uuid": "user-before", "parentUuid": None,
            "isSidechain": False,
            "timestamp": "2026-08-01T00:00:01Z",
            "message": {"role": "user", "content": "before compact"},
        },
        {
            "type": "assistant", "uuid": "assistant-before",
            "parentUuid": "user-before", "isSidechain": False,
            "timestamp": "2026-08-01T00:00:02Z",
            "message": {"role": "assistant", "content": [{
                "type": "text", "text": "old answer",
            }]},
        },
        # This abandoned branch is append-adjacent but not part of the active
        # parent chain. Raw file order must never resurrect it.
        {
            "type": "user", "uuid": "abandoned-user",
            "parentUuid": "user-before", "isSidechain": False,
            "timestamp": "2026-08-01T00:00:03Z",
            "message": {"role": "user", "content": "abandoned branch"},
        },
        {
            "type": "system", "subtype": "compact_boundary",
            "uuid": "compact-boundary", "parentUuid": None,
            "logicalParentUuid": "assistant-before", "isSidechain": False,
            "timestamp": "2026-08-01T00:00:04Z",
            "content": "Conversation compacted",
        },
        {
            "type": "user", "uuid": "compact-summary",
            "parentUuid": "compact-boundary", "isSidechain": False,
            "isCompactSummary": True,
            "timestamp": "2026-08-01T00:00:05Z",
            "message": {
                "role": "user",
                "content": (
                    "This session is being continued from a previous "
                    "conversation that ran out of context.\n\nSummary: hidden"
                ),
            },
        },
        {
            "type": "user", "uuid": "compact-command",
            "parentUuid": "compact-summary", "isSidechain": False,
            "timestamp": "2026-08-01T00:00:06Z",
            "message": {
                "role": "user",
                "content": "<command-name>/compact</command-name>",
            },
        },
        {
            "type": "user", "uuid": "user-after",
            "parentUuid": "compact-command", "isSidechain": False,
            "timestamp": "2026-08-01T00:00:07Z",
            "message": {"role": "user", "content": "after compact"},
        },
        {
            "type": "assistant", "uuid": "assistant-after",
            "parentUuid": "user-after", "isSidechain": False,
            "timestamp": "2026-08-01T00:00:08Z",
            "message": {"role": "assistant", "content": [{
                "type": "text", "text": "new answer",
            }]},
        },
        {
            "type": "assistant", "uuid": "sidechain-tail",
            "parentUuid": "abandoned-user", "isSidechain": True,
            "timestamp": "2026-08-01T00:00:09Z",
            "message": {"role": "assistant", "content": [{
                "type": "text", "text": "private sidechain",
            }]},
        },
    ]
    transcript.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "cc_remote.wrapper.stream.transcript_path",
        lambda _sid: str(transcript),
    )

    recovered = transcript_compact_main_chain("claude-compact")

    assert recovered is not None
    messages, timestamps = recovered
    assert [message.uuid for message in messages] == [
        "user-before", "assistant-before", "compact-summary",
        "compact-command", "user-after", "assistant-after",
    ]
    events = translate_history(messages, 10_000, timestamps=timestamps)
    assert [event.prompt for event in events if isinstance(event, UserMsg)] == [
        "before compact", "after compact",
    ]
    assert timestamps["user-before"] < timestamps["user-after"]


def test_compacted_claude_main_chain_rejects_missing_logical_parent(
        monkeypatch, tmp_path):
    transcript = tmp_path / "broken-compact.jsonl"
    rows = [
        {
            "type": "system", "subtype": "compact_boundary",
            "uuid": "compact-boundary", "parentUuid": None,
            "logicalParentUuid": "missing-precompact-parent",
            "isSidechain": False,
        },
        {
            "type": "user", "uuid": "compact-summary",
            "parentUuid": "compact-boundary", "isSidechain": False,
            "message": {
                "role": "user",
                "content": (
                    "This session is being continued from a previous "
                    "conversation that ran out of context.\n\nSummary: hidden"
                ),
            },
        },
        {
            "type": "user", "uuid": "user-after",
            "parentUuid": "compact-summary", "isSidechain": False,
            "message": {"role": "user", "content": "after compact"},
        },
        {
            "type": "assistant", "uuid": "assistant-after",
            "parentUuid": "user-after", "isSidechain": False,
            "message": {"role": "assistant", "content": [{
                "type": "text", "text": "new answer",
            }]},
        },
    ]
    transcript.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "cc_remote.wrapper.stream.transcript_path",
        lambda _sid: str(transcript),
    )

    assert transcript_compact_main_chain("claude-broken-compact") is None


def test_compact_index_reuses_snapshot_and_scans_only_appended_rows(
        monkeypatch, tmp_path):
    import cc_remote.wrapper.history_store as history_store_module

    transcript = tmp_path / "incremental-compact.jsonl"
    rows = [
        {"type": "user", "uuid": "user-old", "parentUuid": None,
         "isSidechain": False,
         "message": {"role": "user", "content": "old"}},
        {"type": "assistant", "uuid": "answer-old",
         "parentUuid": "user-old", "isSidechain": False,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "answer"}]}},
        {"type": "system", "subtype": "compact_boundary",
         "uuid": "compact-boundary", "parentUuid": None,
         "logicalParentUuid": "answer-old", "isSidechain": False},
        {"type": "user", "uuid": "compact-summary",
         "parentUuid": "compact-boundary", "isSidechain": False,
         "message": {"role": "user", "content": "summary"}},
    ]
    transcript.write_bytes(b"".join(
        (json.dumps(row) + "\n").encode() for row in rows))
    store = HistoryIndexStore(tmp_path / "incremental-state")

    def visible_user(row):
        return row.get("type") == "user"

    initial_size = transcript.stat().st_size
    initial = store.get_claude_compact_index(
        str(transcript), snapshot_size=initial_size,
        max_record_bytes=64 * 1024 * 1024, max_entries=100,
        visible_user=visible_user,
    )
    assert initial is not None
    assert initial.leaf == "compact-summary"

    real_loads = history_store_module.json.loads
    monkeypatch.setattr(
        history_store_module.json, "loads",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an unchanged page must reuse the compact index")),
    )
    assert store.get_claude_compact_index(
        str(transcript), snapshot_size=initial_size,
        max_record_bytes=64 * 1024 * 1024, max_entries=100,
        visible_user=visible_user,
    ) is not None

    parsed = 0

    def count_loads(payload):
        nonlocal parsed
        parsed += 1
        return real_loads(payload)

    monkeypatch.setattr(history_store_module.json, "loads", count_loads)
    appended = {
        "type": "user", "uuid": "user-new",
        "parentUuid": "compact-summary", "isSidechain": False,
        "message": {"role": "user", "content": "new"},
    }
    with transcript.open("ab") as target:
        target.write((json.dumps(appended) + "\n").encode())
    updated = store.get_claude_compact_index(
        str(transcript), snapshot_size=transcript.stat().st_size,
        max_record_bytes=64 * 1024 * 1024, max_entries=100,
        visible_user=visible_user,
    )
    assert updated is not None
    assert updated.leaf == "user-new"
    assert parsed == 1


def test_compact_index_never_authorizes_an_incomplete_snapshot(tmp_path):
    transcript = tmp_path / "growing-compact.jsonl"
    rows = [
        {"type": "user", "uuid": "user-old", "parentUuid": None,
         "isSidechain": False,
         "message": {"role": "user", "content": "old"}},
        {"type": "assistant", "uuid": "answer-old",
         "parentUuid": "user-old", "isSidechain": False,
         "message": {"role": "assistant", "content": []}},
        {"type": "system", "subtype": "compact_boundary",
         "uuid": "compact-boundary", "parentUuid": None,
         "logicalParentUuid": "answer-old", "isSidechain": False},
        {"type": "user", "uuid": "compact-summary",
         "parentUuid": "compact-boundary", "isSidechain": False,
         "message": {"role": "user", "content": "summary"}},
    ]
    transcript.write_bytes(b"".join(
        (json.dumps(row) + "\n").encode() for row in rows))
    store = HistoryIndexStore(tmp_path / "growing-state")

    def visible_user(row):
        return row.get("type") == "user"

    stable_size = transcript.stat().st_size
    assert store.get_claude_compact_index(
        str(transcript), snapshot_size=stable_size,
        max_record_bytes=1024 * 1024, max_entries=100,
        visible_user=visible_user,
    ) is not None

    with transcript.open("ab") as target:
        target.write(b'{"type":"assistant","uuid":"partial')
    assert store.get_claude_compact_index(
        str(transcript), snapshot_size=transcript.stat().st_size,
        max_record_bytes=1024 * 1024, max_entries=100,
        visible_user=visible_user,
    ) is None
    stable = store.get_claude_compact_index(
        str(transcript), snapshot_size=stable_size,
        max_record_bytes=1024 * 1024, max_entries=100,
        visible_user=visible_user,
    )
    assert stable is not None
    assert stable.leaf == "compact-summary"


def test_compact_index_rebuilds_after_truncate_and_inode_replacement(tmp_path):
    transcript = tmp_path / "replaceable-compact.jsonl"
    store = HistoryIndexStore(tmp_path / "replaceable-state")

    def visible_user(row):
        return row.get("type") == "user"

    def write_chain(path, suffix, padding=""):
        rows = [
            {"type": "user", "uuid": f"user-{suffix}", "parentUuid": None,
             "isSidechain": False,
             "message": {"role": "user", "content": padding or suffix}},
            {"type": "assistant", "uuid": f"answer-{suffix}",
             "parentUuid": f"user-{suffix}", "isSidechain": False,
             "message": {"role": "assistant", "content": []}},
            {"type": "system", "subtype": "compact_boundary",
             "uuid": f"compact-{suffix}", "parentUuid": None,
             "logicalParentUuid": f"answer-{suffix}", "isSidechain": False},
            {"type": "user", "uuid": f"summary-{suffix}",
             "parentUuid": f"compact-{suffix}", "isSidechain": False,
             "message": {"role": "user", "content": "summary"}},
        ]
        path.write_bytes(b"".join(
            (json.dumps(row) + "\n").encode() for row in rows))

    write_chain(transcript, "large", "x" * 4096)
    initial = store.get_claude_compact_index(
        str(transcript), snapshot_size=transcript.stat().st_size,
        max_record_bytes=1024 * 1024, max_entries=100,
        visible_user=visible_user,
    )
    assert initial is not None and initial.leaf == "summary-large"

    write_chain(transcript, "small")
    truncated = store.get_claude_compact_index(
        str(transcript), snapshot_size=transcript.stat().st_size,
        max_record_bytes=1024 * 1024, max_entries=100,
        visible_user=visible_user,
    )
    assert truncated is not None and truncated.leaf == "summary-small"
    assert "summary-large" not in truncated.rows

    replacement = tmp_path / "replacement.jsonl"
    write_chain(replacement, "inode")
    os.replace(replacement, transcript)
    replaced = store.get_claude_compact_index(
        str(transcript), snapshot_size=transcript.stat().st_size,
        max_record_bytes=1024 * 1024, max_entries=100,
        visible_user=visible_user,
    )
    assert replaced is not None and replaced.leaf == "summary-inode"
    assert "summary-small" not in replaced.rows


def test_compact_index_loads_large_active_record_and_task_notification(tmp_path):
    transcript = tmp_path / "large-active-compact.jsonl"
    notification = (
        "<task-notification><task-id>task-large</task-id>"
        "<status>completed</status></task-notification>"
    )
    rows = [
        {"type": "queue-operation", "operation": "enqueue",
         "content": notification},
        {"type": "user", "uuid": "user-old", "parentUuid": None,
         "isSidechain": False,
         "message": {"role": "user", "content": "old"}},
        {"type": "assistant", "uuid": "answer-large",
         "parentUuid": "user-old", "isSidechain": False,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "x" * (20 * 1024 * 1024)}]}},
        {"type": "system", "subtype": "compact_boundary",
         "uuid": "compact-boundary", "parentUuid": None,
         "logicalParentUuid": "answer-large", "isSidechain": False},
        {"type": "user", "uuid": "compact-summary",
         "parentUuid": "compact-boundary", "isSidechain": False,
         "message": {"role": "user", "content": "summary"}},
        {"type": "user", "uuid": "task-notification",
         "parentUuid": "compact-summary", "isSidechain": False,
         "origin": {"kind": "task-notification"},
         "message": {"role": "user", "content": notification}},
        {"type": "user", "uuid": "user-new",
         "parentUuid": "task-notification", "isSidechain": False,
         "message": {"role": "user", "content": "new"}},
    ]
    transcript.write_bytes(b"".join(
        (json.dumps(row) + "\n").encode() for row in rows))
    store = HistoryIndexStore(tmp_path / "large-active-state")
    snapshot = transcript_compact_snapshot(
        "claude-compact", path=str(transcript), index_store=store,
        snapshot_size=transcript.stat().st_size,
        max_record_bytes=64 * 1024 * 1024,
    )
    assert snapshot is not None
    messages, _timestamps, internal_events = snapshot
    assert "answer-large" in {message.uuid for message in messages}
    assert len(next(message for message in messages
                    if message.uuid == "answer-large").message["content"][0][
                        "text"]) == 20 * 1024 * 1024
    assert internal_events["task-notification"].kind == "task"


def test_compacted_claude_page_loads_only_requested_main_chain_turns(
        tmp_path):
    transcript = tmp_path / "paged-compact.jsonl"
    rows = [
        {"type": "user", "uuid": "user-1", "parentUuid": None,
         "isSidechain": False,
         "message": {"role": "user", "content": "question 1"}},
        {"type": "assistant", "uuid": "answer-1", "parentUuid": "user-1",
         "isSidechain": False,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "answer 1"}]}},
        {"type": "user", "uuid": "user-2", "parentUuid": "answer-1",
         "isSidechain": False,
         "message": {"role": "user", "content": "question 2"}},
        {"type": "assistant", "uuid": "answer-2", "parentUuid": "user-2",
         "isSidechain": False,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "answer 2"}]}},
        {"type": "system", "subtype": "compact_boundary",
         "uuid": "compact-boundary", "parentUuid": None,
         "logicalParentUuid": "answer-2", "isSidechain": False},
        {"type": "user", "uuid": "compact-summary",
         "parentUuid": "compact-boundary", "isSidechain": False,
         "message": {"role": "user", "content": (
             "This session is being continued from a previous conversation "
             "that ran out of context.\n\nSummary: hidden")}},
        {"type": "user", "uuid": "compact-command",
         "parentUuid": "compact-summary", "isSidechain": False,
         "message": {"role": "user",
                     "content": "<command-name>/compact</command-name>"}},
        {"type": "user", "uuid": "user-3",
         "parentUuid": "compact-command", "isSidechain": False,
         "message": {"role": "user", "content": "question 3"}},
        {"type": "assistant", "uuid": "answer-3", "parentUuid": "user-3",
         "isSidechain": False,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "answer 3"}]}},
        {"type": "user", "uuid": "user-4", "parentUuid": "answer-3",
         "isSidechain": False,
         "message": {"role": "user", "content": "question 4"}},
        {"type": "assistant", "uuid": "answer-4", "parentUuid": "user-4",
         "isSidechain": False,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "answer 4"}]}},
        {"type": "assistant", "uuid": "abandoned-large",
         "parentUuid": "answer-1", "isSidechain": True,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "x" * (1024 * 1024)}]}},
    ]
    transcript.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    newest = transcript_compact_history_page(
        "claude-compact", path=str(transcript), limit=1)
    assert newest is not None
    assert [message.uuid for message in newest.messages] == [
        "user-4", "answer-4",
    ]
    assert newest.has_more is True
    assert newest.oldest_cursor == "user-4"

    older = transcript_compact_history_page(
        "claude-compact", path=str(transcript), before="user-4", limit=2)
    assert older is not None
    assert [message.uuid for message in older.messages] == [
        "user-2", "answer-2", "compact-summary", "compact-command",
        "user-3", "answer-3",
    ]
    assert older.has_more is True
    assert older.oldest_cursor == "user-2"


def test_compacted_claude_page_uses_only_visible_human_boundaries(tmp_path):
    transcript = tmp_path / "internal-compact.jsonl"
    rows = [
        {"type": "user", "uuid": "user-old", "parentUuid": None,
         "isSidechain": False,
         "message": {"role": "user", "content": "old question"}},
        {"type": "assistant", "uuid": "answer-old",
         "parentUuid": "user-old", "isSidechain": False,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "old answer"}]}},
        {"type": "system", "subtype": "compact_boundary",
         "uuid": "compact-boundary", "parentUuid": None,
         "logicalParentUuid": "answer-old", "isSidechain": False},
        {"type": "user", "uuid": "compact-summary",
         "parentUuid": "compact-boundary", "isSidechain": False,
         "message": {"role": "user", "content": (
             "This session is being continued from a previous conversation "
             "that ran out of context.\n\nSummary: hidden")}},
        {"type": "user", "uuid": "task-notification",
         "parentUuid": "compact-summary", "isSidechain": False,
         "origin": {"kind": "task-notification"},
         "message": {"role": "user", "content": (
             "<task-notification><task-id>task-1</task-id>"
             "<status>completed</status></task-notification>")}},
        {"type": "user", "uuid": "blank-user",
         "parentUuid": "task-notification", "isSidechain": False,
         "message": {"role": "user", "content": "   "}},
        {"type": "user", "uuid": "user-new",
         "parentUuid": "blank-user", "isSidechain": False,
         "message": {"role": "user", "content": "new question"}},
        {"type": "assistant", "uuid": "answer-new",
         "parentUuid": "user-new", "isSidechain": False,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "new answer"}]}},
    ]
    transcript.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    older = transcript_compact_history_page(
        "claude-compact", path=str(transcript),
        before="user-new", limit=1,
    )

    assert older is not None
    assert older.oldest_cursor == "user-old"
    assert older.has_more is False
    assert [message.uuid for message in older.messages] == [
        "user-old", "answer-old", "compact-summary",
        "task-notification", "blank-user",
    ]
    exhausted = transcript_compact_history_page(
        "claude-compact", path=str(transcript),
        before="user-old", limit=1,
    )
    assert exhausted is not None
    assert exhausted.messages == []
    assert exhausted.oldest_cursor is None
    assert exhausted.has_more is False
    assert transcript_compact_history_page(
        "claude-compact", path=str(transcript),
        before="not-a-page-cursor", limit=1,
    ) is None


def test_compacted_claude_page_drops_oldest_turns_to_payload_budget(tmp_path):
    transcript = tmp_path / "budgeted-compact.jsonl"
    rows = [
        {"type": "user", "uuid": "user-old", "parentUuid": None,
         "isSidechain": False,
         "message": {"role": "user", "content": "old " + "x" * 400}},
        {"type": "assistant", "uuid": "answer-old",
         "parentUuid": "user-old", "isSidechain": False,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "old answer " + "y" * 400}]}},
        {"type": "system", "subtype": "compact_boundary",
         "uuid": "compact-boundary", "parentUuid": None,
         "logicalParentUuid": "answer-old", "isSidechain": False},
        {"type": "user", "uuid": "compact-summary",
         "parentUuid": "compact-boundary", "isSidechain": False,
         "message": {"role": "user", "content": (
             "This session is being continued from a previous conversation "
             "that ran out of context.\n\nSummary: hidden")}},
        {"type": "user", "uuid": "user-new",
         "parentUuid": "compact-summary", "isSidechain": False,
         "message": {"role": "user", "content": "new question"}},
        {"type": "assistant", "uuid": "answer-new",
         "parentUuid": "user-new", "isSidechain": False,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "new answer"}]}},
    ]
    encoded_rows = [
        (json.dumps(row) + "\n").encode("utf-8") for row in rows
    ]
    transcript.write_bytes(b"".join(encoded_rows))
    newest_payload_bytes = len(encoded_rows[-2]) + len(encoded_rows[-1])

    page = transcript_compact_history_page(
        "claude-compact", path=str(transcript), limit=2,
        max_payload_bytes=newest_payload_bytes,
    )

    assert page is not None
    assert [message.uuid for message in page.messages] == [
        "user-new", "answer-new",
    ]
    assert page.oldest_cursor == "user-new"
    assert page.has_more is True
    assert transcript_compact_history_page(
        "claude-compact", path=str(transcript), limit=1,
        max_payload_bytes=newest_payload_bytes - 1,
    ) is None


def test_compacted_claude_history_pages_across_compact_boundary(
        monkeypatch, tmp_path):
    transcript = tmp_path / "claude-session.jsonl"
    rows = [
        {
            "type": "user", "uuid": "turn-before", "parentUuid": None,
            "isSidechain": False,
            "timestamp": "2026-08-01T00:00:01Z",
            "message": {"role": "user", "content": "before compact"},
        },
        {
            "type": "assistant", "uuid": "answer-before",
            "parentUuid": "turn-before", "isSidechain": False,
            "timestamp": "2026-08-01T00:00:02Z",
            "message": {"role": "assistant", "content": [{
                "type": "text", "text": "old answer",
            }]},
        },
        {
            "type": "system", "subtype": "compact_boundary",
            "uuid": "compact-boundary", "parentUuid": None,
            "logicalParentUuid": "answer-before", "isSidechain": False,
            "timestamp": "2026-08-01T00:00:03Z",
        },
        {
            "type": "user", "uuid": "compact-summary",
            "parentUuid": "compact-boundary", "isSidechain": False,
            "isCompactSummary": True,
            "timestamp": "2026-08-01T00:00:04Z",
            "message": {
                "role": "user",
                "content": (
                    "This session is being continued from a previous "
                    "conversation that ran out of context.\n\nSummary: hidden"
                ),
            },
        },
        {
            "type": "user", "uuid": "turn-after",
            "parentUuid": "compact-summary", "isSidechain": False,
            "timestamp": "2026-08-01T00:00:05Z",
            "message": {"role": "user", "content": "after compact"},
        },
        {
            "type": "assistant", "uuid": "answer-after",
            "parentUuid": "turn-after", "isSidechain": False,
            "timestamp": "2026-08-01T00:00:06Z",
            "message": {"role": "assistant", "content": [{
                "type": "text", "text": "new answer",
            }]},
        },
    ]
    transcript.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    monkeypatch.setattr(mm, "transcript_path", lambda _sid: str(transcript))
    monkeypatch.setattr(
        "cc_remote.wrapper.stream.transcript_path",
        lambda _sid: str(transcript),
    )
    monkeypatch.setattr(mm, "get_session_info", lambda _sid: None)
    # Characterize the SDK limitation: after compact it exposes only the new
    # ancestry. _build_history must prefer the raw logical-parent chain.
    monkeypatch.setattr(
        mm,
        "get_session_messages",
        lambda *_args, **_kwargs: [SimpleNamespace(
            uuid="turn-after", type="user",
            message={"role": "user", "content": "after compact"},
            parent_tool_use_id=None,
        )],
    )
    monkeypatch.setattr(mm, "translate_subagent_history", lambda *_args: [])

    async def go():
        machine, _ = _mk_machine()
        machine._history_index = HistoryIndexStore(tmp_path / "compact-state")
        newest = await machine._build_history(
            "claude-compact", limit=1, detail="summary")
        assert [turn.prompt for turn in newest.turns] == ["after compact"]
        assert newest.has_more is True

        older = await machine._build_history(
            "claude-compact", before="turn-after", limit=1,
            detail="summary",
        )
        assert [turn.prompt for turn in older.turns] == ["before compact"]
        assert older.has_more is False

    asyncio.run(go())


def test_scoped_empty_claude_history_retries_global_lookup(
        monkeypatch, tmp_path):
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text("{}\n")
    calls = []
    canned = [
        UserMsg(msg_id="turn-1", prompt="recovered"),
        TurnEnd(result=TurnResult(
            subtype="success", duration_ms=1, is_error=False)),
    ]
    monkeypatch.setattr(mm, "transcript_path", lambda _sid: str(transcript))
    monkeypatch.setattr(mm, "get_session_info", lambda _sid: None)

    def messages(_sid, directory=None):
        calls.append(directory)
        return [] if directory else ["global-message"]

    monkeypatch.setattr(mm, "get_session_messages", messages)
    monkeypatch.setattr(mm, "transcript_timestamps", lambda _sid: {})
    monkeypatch.setattr(
        mm, "transcript_internal_user_events", lambda _sid: [])
    monkeypatch.setattr(
        mm, "translate_history",
        lambda *_args, **_kwargs: [event.model_copy() for event in canned])
    monkeypatch.setattr(mm, "translate_subagent_history", lambda *_args: [])
    monkeypatch.setattr(mm, "last_assistant_model", lambda _msgs: None)

    async def go():
        machine, _ = _mk_machine()
        history = await machine._build_history(
            "claude-session", limit=4, cwd_hint="/stale/browser")

        assert [row["prompt"] for row in history.events
                if row["type"] == "user_msg"] == ["recovered"]
        # _build_history resolves the hint with os.path.realpath, which on
        # Windows also rewrites a POSIX-style absolute path to the current
        # drive (e.g. "/stale/browser" -> "D:\\stale\\browser").
        assert calls == [os.path.realpath("/stale/browser"), None]

    asyncio.run(go())


def test_truly_empty_claude_history_is_authoritative_after_global_retry(
        monkeypatch, tmp_path):
    transcript = tmp_path / "claude-empty.jsonl"
    transcript.write_text("{}\n")
    calls = []
    monkeypatch.setattr(mm, "transcript_path", lambda _sid: str(transcript))
    monkeypatch.setattr(mm, "get_session_info", lambda _sid: None)

    def messages(_sid, directory=None):
        calls.append(directory)
        return []

    monkeypatch.setattr(mm, "get_session_messages", messages)
    monkeypatch.setattr(mm, "transcript_timestamps", lambda _sid: {})
    monkeypatch.setattr(
        mm, "transcript_internal_user_events", lambda _sid: [])
    monkeypatch.setattr(mm, "translate_history", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(mm, "translate_subagent_history", lambda *_args: [])
    monkeypatch.setattr(mm, "last_assistant_model", lambda _msgs: None)

    async def go():
        machine, _ = _mk_machine()
        machine._history_index = HistoryIndexStore(tmp_path / "state-empty")
        history = await machine._build_history(
            "claude-empty", limit=4, cwd_hint="/listed/project")
        source = HistorySourceFingerprint.capture(transcript)

        assert history.authoritative is True
        assert history.events == []
        # See the realpath note in test_scoped_empty_claude_history_retries_global_lookup.
        assert calls == [os.path.realpath("/listed/project"), None]
        cached = machine._history_index.get_page(
            "claude-empty", "claude", source, before=None, limit=4)
        assert cached is not None and cached.turns == ()

    asyncio.run(go())


def test_claude_history_read_failure_never_materializes_empty_page(
        monkeypatch, tmp_path):
    transcript = tmp_path / "claude-error.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setattr(mm, "transcript_path", lambda _sid: str(transcript))
    monkeypatch.setattr(
        mm, "get_session_info",
        lambda _sid: SimpleNamespace(cwd="/authoritative/project"),
    )
    monkeypatch.setattr(
        mm, "get_session_messages",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("transcript unavailable")),
    )

    async def go():
        machine, _ = _mk_machine()
        machine._history_index = HistoryIndexStore(tmp_path / "state-error")
        history = await machine._build_history("claude-error", limit=4)
        source = HistorySourceFingerprint.capture(transcript)

        assert history.authoritative is False
        assert history.events == []
        assert machine._history_index.get_page(
            "claude-error", "claude", source,
            before=None, limit=4,
        ) is None

    asyncio.run(go())


def test_claude_history_refresh_coalesces_appends_during_full_scan(monkeypatch):
    async def go():
        machine, transport = _mk_machine()
        machine.HISTORY_REFRESH_MIN_INTERVAL_SECONDS = 0
        entered = asyncio.Event()
        release = asyncio.Event()
        builds = 0

        async def build(sid, **_kwargs):
            nonlocal builds
            builds += 1
            if builds == 1:
                entered.set()
                await release.wait()
            return History(
                session_id=sid,
                revision="refresh-rev",
                events=[],
                turns=[],
                detail="summary",
                has_more=False,
            )

        monkeypatch.setattr(machine, "_build_history", build)
        args = {
            "before": None, "limit": 4, "cwd": "/repo", "detail": "summary",
        }
        machine._schedule_history_refresh("claude-refresh", **args)
        await entered.wait()
        machine._schedule_history_refresh("claude-refresh", **args)
        machine._schedule_history_refresh("claude-refresh", **args)
        release.set()
        await asyncio.gather(*list(machine._history_refresh_tasks.values()))

        assert builds == 2
        assert len([row for row in transport.sent
                    if isinstance(row, History)]) == 2

    asyncio.run(go())


def test_codex_history_refresh_coalesces_cwd_hints_and_rate_limits_rescan(
        monkeypatch):
    async def go():
        machine, transport = _mk_machine()
        machine.HISTORY_REFRESH_MIN_INTERVAL_SECONDS = 0.03
        ctx = _mk_ctx("codex-refresh", "codex-refresh")
        ctx.engine = "codex"
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx
        entered = asyncio.Event()
        release = asyncio.Event()
        starts = []

        async def build(sid, **_kwargs):
            starts.append(time.monotonic())
            if len(starts) == 1:
                entered.set()
                await release.wait()
            return History(
                session_id=sid,
                revision=f"refresh-{len(starts)}",
                events=[],
                turns=[],
                detail="summary",
                has_more=False,
            )

        monkeypatch.setattr(machine, "_build_history", build)
        args = {
            "before": None, "limit": 4, "detail": "summary",
        }
        machine._schedule_history_refresh(
            "codex-refresh", cwd="/client/a", **args)
        await entered.wait()
        machine._schedule_history_refresh(
            "codex-refresh", cwd="/client/b", **args)
        machine._schedule_history_refresh(
            "codex-refresh", cwd="/client/c", **args)

        assert len(machine._history_refresh_tasks) == 1
        release.set()
        await asyncio.gather(*list(machine._history_refresh_tasks.values()))

        assert len(starts) == 2
        assert starts[1] - starts[0] >= 0.02
        assert len([row for row in transport.sent
                    if isinstance(row, History)]) == 2

    asyncio.run(go())


def test_history_refresh_backoff_scales_with_scan_cost():
    machine, _ = _mk_machine()
    machine.HISTORY_REFRESH_MIN_INTERVAL_SECONDS = 0.5
    machine.HISTORY_REFRESH_MAX_INTERVAL_SECONDS = 10.0

    assert machine._history_refresh_backoff_seconds(0.1) == 0.5
    assert machine._history_refresh_backoff_seconds(3.0) == 3.0
    assert machine._history_refresh_backoff_seconds(30.0) == 10.0


def test_history_refresh_skips_backoff_for_final_idle_rebuild(monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        machine.HISTORY_REFRESH_MIN_INTERVAL_SECONDS = 10.0
        machine.HISTORY_REFRESH_MAX_INTERVAL_SECONDS = 10.0
        ctx = _mk_ctx("codex-final-refresh", "codex-final-refresh")
        ctx.engine = "codex"
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx
        entered = asyncio.Event()
        release = asyncio.Event()
        builds = 0

        async def build(sid, **_kwargs):
            nonlocal builds
            builds += 1
            if builds == 1:
                entered.set()
                await release.wait()
            return History(
                session_id=sid,
                revision=f"refresh-{builds}",
                events=[],
                turns=[],
                detail="summary",
                has_more=False,
            )

        monkeypatch.setattr(machine, "_build_history", build)
        args = {
            "before": None, "limit": 4, "cwd": None, "detail": "summary",
        }
        machine._schedule_history_refresh("codex-final-refresh", **args)
        await entered.wait()
        machine._schedule_history_refresh("codex-final-refresh", **args)
        ctx.state = "idle"
        release.set()

        await asyncio.wait_for(
            asyncio.gather(*list(machine._history_refresh_tasks.values())),
            timeout=0.5,
        )
        assert builds == 2

    asyncio.run(go())


def test_history_refresh_retries_provisional_source_drift_without_dirty_signal(
        monkeypatch):
    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("codex-drift-refresh", "codex-drift-refresh")
        ctx.engine = "codex"
        ctx.state = "idle"
        machine.sessions[ctx.key] = ctx
        builds = 0

        async def build(sid, **_kwargs):
            nonlocal builds
            builds += 1
            return History(
                session_id=sid,
                revision=f"refresh-{builds}",
                authoritative=builds > 1,
                events=[],
                turns=[],
                detail="summary",
                has_more=False,
            )

        monkeypatch.setattr(machine, "_build_history", build)
        machine._schedule_history_refresh(
            "codex-drift-refresh",
            before=None,
            limit=4,
            cwd=None,
            detail="summary",
        )
        await asyncio.wait_for(
            asyncio.gather(*list(machine._history_refresh_tasks.values())),
            timeout=0.5,
        )

        assert builds == 2
        sent = [row for row in transport.sent if isinstance(row, History)]
        assert len(sent) == 1
        assert sent[0].revision == "refresh-2"
        assert sent[0].authoritative is True

    asyncio.run(go())


def test_history_refresh_rate_limits_repeated_drift_when_activity_is_unknown(
        monkeypatch):
    async def go():
        machine, transport = _mk_machine()
        machine.HISTORY_REFRESH_MIN_INTERVAL_SECONDS = 0.03
        machine.HISTORY_REFRESH_MAX_INTERVAL_SECONDS = 0.03
        starts = []

        async def build(sid, **_kwargs):
            starts.append(time.monotonic())
            return History(
                session_id=sid,
                revision=f"refresh-{len(starts)}",
                authoritative=len(starts) > 2,
                events=[],
                turns=[],
                detail="summary",
                has_more=False,
            )

        monkeypatch.setattr(machine, "_build_history", build)
        machine._schedule_history_refresh(
            "unknown-drift-refresh",
            before=None,
            limit=4,
            cwd=None,
            detail="summary",
        )
        await asyncio.wait_for(
            asyncio.gather(*list(machine._history_refresh_tasks.values())),
            timeout=0.5,
        )

        assert len(starts) == 3
        assert starts[2] - starts[1] >= 0.02
        sent = [row for row in transport.sent if isinstance(row, History)]
        assert len(sent) == 1
        assert sent[0].revision == "refresh-3"

    asyncio.run(go())


def test_external_codex_turn_is_history_activity_not_resident_state(monkeypatch):
    """A mirrored native turn marks History active without owning Stop."""
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: None)

    async def go():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("external-codex", "external-codex")
        ctx.engine = "codex"
        ctx.state = "idle"
        machine.sessions[ctx.key] = ctx
        machine._watch["external-codex"] = {
            "engine": "codex",
            "external": True,
            "active_external_turns": {"native-turn": 1.0},
            "takeover_pending": None,
        }

        history = await machine._build_history("external-codex", limit=20)

        assert history.external is True
        assert history.in_progress is True
        assert ctx.state == "idle"

    asyncio.run(go())


def test_active_external_codex_history_marks_growing_snapshot(monkeypatch, tmp_path):
    rollout = tmp_path / "active-external.jsonl"
    rollout.write_text(json.dumps({
        "timestamp": "2026-01-01T00:00:00Z",
        "type": "session_meta",
        "payload": {"id": "external-codex"},
    }) + "\n")
    monkeypatch.setattr(
        mm, "codex_rollout_path", lambda _sid: str(rollout))
    translate_kwargs = []

    def translate(*_args, **kwargs):
        translate_kwargs.append(kwargs)
        return [], None

    monkeypatch.setattr(mm, "codex_translate_history", translate)

    async def go():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("external-codex", "external-codex")
        ctx.engine = "codex"
        ctx.state = "idle"
        machine.sessions[ctx.key] = ctx
        machine._watch["external-codex"] = {
            "engine": "codex",
            "external": True,
            "active_external_turns": {"native-turn": 1.0},
            "takeover_pending": None,
        }

        await machine._build_history("external-codex", limit=20)

        assert translate_kwargs
        assert translate_kwargs[0]["snapshot_in_progress"] is True

    asyncio.run(go())


def test_inflight_history_keeps_pre_rollback_revision(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def delayed_messages(_sid, directory=None):
        entered.set()
        assert release.wait(2)
        return []

    monkeypatch.setattr(mm, "transcript_path", lambda _sid: None)
    monkeypatch.setattr(mm, "get_session_messages", delayed_messages)
    monkeypatch.setattr(mm, "transcript_timestamps", lambda _sid: {})
    monkeypatch.setattr(mm, "translate_history", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(mm, "translate_subagent_history", lambda *_args: [])

    async def go():
        machine, _ = _mk_machine()
        before = machine._history_revision("s1")
        task = asyncio.create_task(machine._build_history("s1", limit=20))
        assert await asyncio.to_thread(entered.wait, 2)
        after = machine._bump_history_revision("s1")
        release.set()
        history = await task
        assert history.revision == before
        assert history.revision != after

    asyncio.run(go())


def test_newest_history_builds_are_monotonic_and_capture_live_seq(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def delayed_first(_sid, directory=None):
        nonlocal calls
        with calls_lock:
            calls += 1
            current = calls
        if current == 1:
            entered.set()
            assert release.wait(2)
        return []

    monkeypatch.setattr(mm, "transcript_path", lambda _sid: None)
    monkeypatch.setattr(mm, "get_session_messages", delayed_first)
    monkeypatch.setattr(mm, "transcript_timestamps", lambda _sid: {})
    monkeypatch.setattr(mm, "translate_history", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(mm, "translate_subagent_history", lambda *_args: [])

    async def go():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("s1", "s1")
        ctx.seq = 7
        machine.sessions["s1"] = ctx

        older_task = asyncio.create_task(machine._build_history("s1", limit=20))
        assert await asyncio.to_thread(entered.wait, 2)
        ctx.seq = 8
        newer = await machine._build_history("s1", limit=20)
        page = await machine._build_history("s1", before="older", limit=20)
        release.set()
        older = await older_task

        assert older.build_seq < newer.build_seq
        assert older.live_seq == 7
        assert newer.live_seq == 8
        assert page.build_seq == newer.build_seq

    asyncio.run(go())


def test_hello_sends_snapshots_and_control_state_without_replay_flood():
    """Hello sends one snapshot plus authoritative control state per resident
    session, but no buffered narrative replay."""
    async def go():
        m, tr = _mk_machine()
        for key in ("s1", "s2"):
            ctx = _mk_ctx(key, key)
            for i in range(5):
                ev = UserMsg(msg_id=f"{key}-{i}", prompt="x")
                ev.seq = ctx.next_seq()
                ev.sid = key
                ctx.buffer.append(ev)
            m.sessions[key] = ctx
        await m._handle_client_hello(SimpleNamespace(client_id="c1"))
        types = [msg.type for msg in tr.sent]
        assert types == [
            "snapshot", "query_queue", "perm",
            "snapshot", "query_queue", "perm",
        ]
        assert "replay_start" not in types and "user_msg" not in types
        assert all(msg.to == "c1" for msg in tr.sent)     # routed to the requesting client
    asyncio.run(go())


def test_hello_with_cursor_replays_only_missing_tail():
    async def go():
        m, tr = _mk_machine()
        ctx = _mk_ctx("s1", "s1")
        for i in range(1, 4):
            ev = UserMsg(msg_id=f"m{i}", prompt="x")
            ev.seq = i
            ctx.seq = i
            ev.sid = "s1"
            ctx.buffer.append(ev)
        m.sessions["s1"] = ctx

        await m._handle_client_hello(SimpleNamespace(
            client_id="c1", cursors={"s1": 2},
            generations={"s1": m.instance_id}, last_seq=None))

        assert [msg.type for msg in tr.sent] == [
            "replay_start", "user_msg", "replay_end", "session_control",
            "query_queue", "perm"]
        assert tr.sent[1].msg_id == "m3"
        assert all(msg.to == "c1" for msg in tr.sent)

    asyncio.run(go())


def test_fresh_hello_replays_only_current_inflight_turn_after_snapshot():
    async def go():
        m, tr = _mk_machine()
        ctx = _mk_ctx("s1", "s1")
        ctx.state = "running"
        for i, prompt in enumerate(("old", "current"), 1):
            ev = UserMsg(msg_id=f"m{i}", prompt=prompt)
            ev.seq = ctx.next_seq()
            ctx.buffer.append(ev)
            delta = Delta(message_id=f"a{i}", text=prompt)
            delta.seq = ctx.next_seq()
            ctx.buffer.append(delta)
        m.sessions["s1"] = ctx

        await m._handle_client_hello(SimpleNamespace(
            client_id="c1", cursors=None, generations=None, last_seq=None))

        assert [msg.type for msg in tr.sent] == [
            "snapshot", "replay_start", "user_msg", "delta", "replay_end",
            "query_queue", "perm"]
        assert tr.sent[2].prompt == "current"
        assert all(msg.to == "c1" for msg in tr.sent)

    asyncio.run(go())


def test_get_history_returns_one_bulk_frame(monkeypatch):
    canned = [
        UserMsg(msg_id="u1", prompt="hi"),
        AssistantMsgStart(message_id="a1"),
        Delta(message_id="a1", text="hello"),
        TurnEnd(result=TurnResult(subtype="success", duration_ms=0, is_error=False)),
    ]
    monkeypatch.setattr(mm, "get_session_messages", lambda sid, directory=None: ["m"])
    monkeypatch.setattr(mm, "translate_history", lambda msgs, mx, timestamps=None: [e.model_copy() for e in canned])
    # A proxy transcript may expose its upstream model. The resident SDK control
    # state remains authoritative for both the selected Claude alias and effort.
    monkeypatch.setattr(mm, "last_assistant_model", lambda msgs: "glm-5.2")

    async def go():
        m, tr = _mk_machine()
        ctx = _mk_ctx("sX", "sX")
        ctx.sdk = SimpleNamespace(model="claude-opus-4-8", effort="max")
        ctx.state = "running"
        m.sessions["sX"] = ctx
        await m._handle_get_history(SimpleNamespace(
            session_id="sX", client_id="c1", cwd="/tmp/x", type="get_history"))
        assert len(tr.sent) == 1                          # ONE bulk frame, not N tiny frames
        hist = tr.sent[0]
        assert hist.type == "history" and hist.session_id == "sX" and hist.to == "c1"
        assert hist.has_more is False
        assert hist.in_progress is True
        assert hist.oldest_id == "u1" and hist.newest_id == "u1"
        # Current model + effort precede the translated transcript narrative.
        assert [(event["type"], event.get("model") or event.get("effort"))
                for event in hist.events[:2]] == [
                    ("model", "claude-opus-4-8"), ("effort", "max")]
        assert [e["type"] for e in hist.events[2:]] == [
            "user_msg", "assistant_msg_start", "delta", "turn_end"]
        # every event is stamped with the session id so the client routes them right
        assert all(e["sid"] == "sX" for e in hist.events)
    asyncio.run(go())


def test_oversized_single_turn_wire_compaction_keeps_source_complete_detail(
        monkeypatch, tmp_path):
    canned = [
        UserMsg(msg_id="u1", prompt="hi"),
        AssistantMsgStart(message_id="a1"),
        Delta(message_id="a1", text="x" * 200_000),
        TurnEnd(result=TurnResult(
            subtype="success", duration_ms=1, is_error=False)),
    ]
    monkeypatch.setattr(mm, "get_session_messages", lambda sid, directory=None: ["m"])
    monkeypatch.setattr(
        mm, "translate_history",
        lambda msgs, mx, timestamps=None: [event.model_copy() for event in canned],
    )
    monkeypatch.setattr(mm, "last_assistant_model", lambda msgs: None)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setattr(mm, "transcript_path", lambda _sid: str(transcript))

    async def go():
        machine, _ = _mk_machine()
        machine.cfg.ws_max_size_bytes = 64 * 1024
        machine._history_index = HistoryIndexStore(tmp_path / "state")
        history = await machine._build_history("s1", limit=60)
        assert len(history.model_dump_json().encode()) < machine.cfg.ws_max_size_bytes
        assert any(row["type"] == "error" for row in history.events)
        detail = machine._history_index.get_turn_detail(
            "s1",
            "claude",
            HistorySourceFingerprint.capture(transcript),
            "u1",
        )
        assert detail is not None
        delta = next(row for row in detail if row["type"] == "delta")
        assert delta["text"] == "x" * 200_000

    asyncio.run(go())


def test_many_turn_history_shrinks_with_logarithmic_serializations(monkeypatch):
    canned = []
    for index in range(256):
        uid = f"u{index}"
        canned.extend([
            UserMsg(msg_id=uid, prompt="q"),
            Delta(message_id=f"a{index}", text="x" * 4096),
            TurnEnd(result=TurnResult(
                subtype="success", duration_ms=1, is_error=False)),
        ])
    monkeypatch.setattr(mm, "get_session_messages", lambda sid, directory=None: ["m"])
    monkeypatch.setattr(
        mm, "translate_history",
        lambda msgs, mx, timestamps=None: [event.model_copy() for event in canned],
    )
    monkeypatch.setattr(mm, "last_assistant_model", lambda msgs: None)

    calls = 0
    original = History.model_dump_json

    def counted(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(History, "model_dump_json", counted)

    async def go():
        machine, _ = _mk_machine()
        machine.cfg.ws_max_size_bytes = 64 * 1024
        history = await machine._build_history("s1")
        assert history.has_more is True
        assert history.newest_id == "u255"
        assert len(history.model_dump_json().encode()) < machine.cfg.ws_max_size_bytes

    asyncio.run(go())
    # 256 linear removals were the prior failure mode. Binary search plus final
    # assertions stays comfortably below this fixed ceiling.
    assert calls < 20


def test_oversized_transcript_is_rejected_before_full_parse(monkeypatch, tmp_path):
    source = tmp_path / "huge.jsonl"
    source.write_text("{}\n")
    parsed = False

    def should_not_parse(*args, **kwargs):
        nonlocal parsed
        parsed = True
        raise AssertionError("history parser should not run")

    monkeypatch.setattr(mm, "transcript_path", lambda sid: str(source))
    monkeypatch.setattr(mm.os.path, "getsize", lambda path: 100_000_000)
    monkeypatch.setattr(mm, "get_session_messages", should_not_parse)

    async def go():
        machine, _ = _mk_machine()
        machine.cfg.history_source_max_bytes = 64 * 1024 * 1024
        history = await machine._build_history("s1", limit=60)
        assert history.events[0]["type"] == "error"
        assert "HISTORY_SOURCE_MAX_BYTES" in history.events[0]["message"]

    asyncio.run(go())
    assert parsed is False


def test_oversized_compacted_claude_transcript_uses_bounded_turn_pages(
        monkeypatch, tmp_path):
    source = tmp_path / "huge-compact.jsonl"
    rows = [
        {"type": "user", "uuid": "user-old", "parentUuid": None,
         "isSidechain": False,
         "message": {"role": "user", "content": "old question"}},
        {"type": "assistant", "uuid": "answer-old",
         "parentUuid": "user-old", "isSidechain": False,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "old answer"}]}},
        {"type": "system", "subtype": "compact_boundary",
         "uuid": "compact-boundary", "parentUuid": None,
         "logicalParentUuid": "answer-old", "isSidechain": False},
        {"type": "user", "uuid": "compact-summary",
         "parentUuid": "compact-boundary", "isSidechain": False,
         "message": {"role": "user", "content": (
             "This session is being continued from a previous conversation "
             "that ran out of context.\n\nSummary: hidden")}},
        {"type": "user", "uuid": "user-new",
         "parentUuid": "compact-summary", "isSidechain": False,
         "message": {"role": "user", "content": "new question"}},
        {"type": "assistant", "uuid": "answer-new",
         "parentUuid": "user-new", "isSidechain": False,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "new answer"}]}},
    ]
    source.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    parsed_by_sdk = False

    def should_not_parse(*_args, **_kwargs):
        nonlocal parsed_by_sdk
        parsed_by_sdk = True
        raise AssertionError("oversized compact history must not use SDK parse")

    monkeypatch.setattr(mm, "transcript_path", lambda _sid: str(source))
    monkeypatch.setattr(
        "cc_remote.wrapper.stream.transcript_path",
        lambda _sid: str(source),
    )
    monkeypatch.setattr(mm.os.path, "getsize", lambda _path: 100_000_000)
    monkeypatch.setattr(mm, "get_session_info", lambda _sid: None)
    monkeypatch.setattr(mm, "get_session_messages", should_not_parse)
    monkeypatch.setattr(mm, "translate_subagent_history", lambda *_args: [])

    async def go():
        machine, _ = _mk_machine()
        machine.cfg.history_source_max_bytes = 64 * 1024 * 1024
        machine._history_index = HistoryIndexStore(tmp_path / "huge-state")

        newest = await machine._build_history(
            "claude-compact", limit=1, detail="summary")
        assert [turn.prompt for turn in newest.turns] == ["new question"]
        assert newest.oldest_id == "user-new"
        assert newest.has_more is True

        older = await machine._build_history(
            "claude-compact", before="user-new", limit=1,
            detail="summary",
        )
        assert [turn.prompt for turn in older.turns] == ["old question"]
        assert older.oldest_id == "user-old"
        assert older.has_more is False

        exhausted = await machine._build_history(
            "claude-compact", before="user-old", limit=1,
            detail="summary",
        )
        assert exhausted.turns == []
        assert exhausted.oldest_id is None
        assert exhausted.has_more is False

        stale = await machine._build_history(
            "claude-compact", before="stale-cursor", limit=1,
            detail="summary",
        )
        assert stale.events[0]["type"] == "error"
        assert "游标已失效" in stale.events[0]["message"]
        assert "HISTORY_SOURCE_MAX_BYTES" not in stale.events[0]["message"]

    asyncio.run(go())
    assert parsed_by_sdk is False


def test_oversized_codex_rollout_reads_recent_turn_window(monkeypatch, tmp_path):
    source = tmp_path / "huge-rollout.jsonl"
    with source.open("wb") as rollout:
        # A sparse prefix makes the source larger than the Claude transcript
        # safety cap without allocating or parsing a giant test fixture.
        rollout.seek(70 * 1024 * 1024)
        rollout.write(b"\n")
        for index in range(1, 4):
            for row in (
                {"timestamp": f"2026-01-01T00:0{index}:01Z",
                 "type": "event_msg",
                 "payload": {"type": "task_started",
                             "turn_id": f"turn-{index}"}},
                {"timestamp": f"2026-01-01T00:0{index}:02Z",
                 "type": "event_msg",
                 "payload": {"type": "user_message",
                             "message": f"question {index}"}},
                {"timestamp": f"2026-01-01T00:0{index}:03Z",
                 "type": "event_msg",
                 "payload": {"type": "agent_message",
                             "message": f"answer {index}"}},
                {"timestamp": f"2026-01-01T00:0{index}:04Z",
                 "type": "event_msg",
                 "payload": {"type": "task_complete",
                             "turn_id": f"turn-{index}"}},
            ):
                rollout.write((json.dumps(row) + "\n").encode())

    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(source))

    async def go():
        machine, _ = _mk_machine()
        machine.cfg.history_source_max_bytes = 64 * 1024 * 1024
        ctx = _mk_ctx("codex-large", "codex-large")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx

        newest = await machine._build_history("codex-large", limit=2)
        assert [row["prompt"] for row in newest.events
                if row["type"] == "user_msg"] == ["question 2", "question 3"]
        assert newest.oldest_id == "turn-2"
        assert newest.newest_id == "turn-3"
        assert newest.has_more is True
        assert not any("HISTORY_SOURCE_MAX_BYTES" in row.get("message", "")
                       for row in newest.events)

    asyncio.run(go())


def test_compacted_codex_tail_recovers_omitted_current_prompt(
        monkeypatch, tmp_path):
    source = tmp_path / "compacted-rollout.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "codex-compacted"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-long"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "user_message",
                     "message": "the prompt before compact"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "commentary",
                     "message": "work before compact"}},
        # One compact record is larger than the configured source window. The
        # newest-page translator therefore starts after it, just like a real
        # multi-hundred-MiB turn, while the prompt remains recoverable from the
        # already-discovered task boundary.
        {"timestamp": "2026-01-01T00:00:04Z", "type": "compacted",
         "payload": {"replacement_history": ["x" * (1100 * 1024)]}},
        {"timestamp": "2026-01-01T00:00:05Z", "type": "event_msg",
         "payload": {"type": "context_compacted"}},
        {"timestamp": "2026-01-01T00:00:06Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "final_answer",
                     "message": "answer after compact"}},
        {"timestamp": "2026-01-01T00:00:07Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-long"}},
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(source))

    async def go():
        machine, _ = _mk_machine()
        machine.cfg.codex_history_window_max_bytes = 1024 * 1024
        ctx = _mk_ctx("codex-compacted", "codex-compacted")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx

        newest = await machine._build_history("codex-compacted", limit=60)
        assert [row["prompt"] for row in newest.events
                if row["type"] == "user_msg"] == [
                    "the prompt before compact"
                ]
        assert any(row.get("type") == "delta"
                   and "answer after compact" in row.get("text", "")
                   for row in newest.events)
        assert newest.oldest_id == "turn-long"
        assert newest.has_more is True

    asyncio.run(go())


def test_compact_continuation_split_from_terminal_is_not_an_error_turn(
        tmp_path):
    """A compact-continuation turn (turn_context + context_compacted + visible
    assistant content) that never reaches its terminal record before the next
    user_message — e.g. a history page that split the continuation from its
    task_complete — must close as a normal truncated turn, not a synthetic
    error turn."""
    from cc_remote.wrapper.codex_stream import codex_translate_history
    source = tmp_path / "compact-dangling.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "s"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "t1"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "first"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "final_answer",
                     "message": "first answer"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "t1"}},
        # compact continuation with visible content but NO terminal record
        # before the next user turn (mimics a page split).
        {"timestamp": "2026-01-01T00:00:05Z", "type": "turn_context",
         "payload": {"turn_id": "t2"}},
        {"timestamp": "2026-01-01T00:00:06Z", "type": "event_msg",
         "payload": {"type": "context_compacted"}},
        {"timestamp": "2026-01-01T00:00:07Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "final_answer",
                     "message": "continuation answer"}},
        # next user turn closes the dangling continuation.
        {"timestamp": "2026-01-01T00:00:08Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "second"}},
        {"timestamp": "2026-01-01T00:00:09Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "final_answer",
                     "message": "second answer"}},
        {"timestamp": "2026-01-01T00:00:10Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "t3"}},
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))
    events, _ = codex_translate_history(str(source), tool_result_max=4096)
    turn_ends = [e for e in events if type(e).__name__ == "TurnEnd"]
    assert [e.turn_id for e in turn_ends] == ["t1", None, "t3"]
    assert [e.result.subtype for e in turn_ends] == [
        "success", "success", "success"]
    assert [e.result.is_error for e in turn_ends] == [False, False, False]


def test_context_compacted_before_user_belongs_to_that_user_turn(tmp_path):
    """A task can compact before Codex records its clean user_message.

    The compact marker is process metadata for the upcoming user turn, not an
    empty assistant-only turn that should be closed as a synthetic error.
    """
    from cc_remote.wrapper.codex_stream import codex_translate_history

    source = tmp_path / "compact-before-user.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "s"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-1"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "compacted",
         "payload": {"replacement_history": []}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "context_compacted"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "turn_context",
         "payload": {"turn_id": "turn-1"}},
        {"timestamp": "2026-01-01T00:00:05Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "continue"}},
        {"timestamp": "2026-01-01T00:00:06Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "final_answer",
                     "message": "done"}},
        {"timestamp": "2026-01-01T00:00:06Z", "type": "response_item",
         "payload": {"type": "message", "id": "message-1",
                     "role": "assistant", "phase": "final_answer",
                     "content": [{"type": "output_text", "text": "done"}]}},
        {"timestamp": "2026-01-01T00:00:07Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-1",
                     "last_agent_message": "done"}},
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(str(source), tool_result_max=4096)
    user_index = next(
        index for index, event in enumerate(events)
        if isinstance(event, UserMsg)
    )
    compact_index = next(
        index for index, event in enumerate(events)
        if isinstance(event, ProcessEvent) and event.kind == "compaction"
    )
    assert compact_index > user_index
    assert events[compact_index].turn_id == "turn-1"
    assert events[compact_index].ts == 1767225603.0
    assert len([event for event in events if isinstance(event, TurnEnd)]) == 1
    assert not any(isinstance(event, Error) for event in events)

    turns = materialize_history_turns([
        event.model_dump(mode="json") for event in events
    ], include_live_detail=True)
    assert [(turn["prompt"], turn["done"], turn.get("error"))
            for turn in turns] == [("continue", True, None)]
    assert any(
        block.get("processKind") == "compaction"
        for block in turns[0]["blocks"]
    )


def test_pending_compaction_does_not_cross_a_new_task_owner(tmp_path):
    from cc_remote.wrapper.codex_stream import codex_translate_history

    source = tmp_path / "compact-owner.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "old-turn"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "context_compacted"}},
        # No visible output or terminal ever materialized old-turn. Its marker
        # must not leak into the next authoritative task.
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "new-turn"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "new prompt"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "final_answer",
                     "message": "new answer"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "response_item",
         "payload": {"type": "message", "id": "new-message",
                     "role": "assistant", "phase": "final_answer",
                     "content": [{
                         "type": "output_text", "text": "new answer",
                     }]}},
        {"timestamp": "2026-01-01T00:00:05Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "new-turn",
                     "last_agent_message": "new answer"}},
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(str(source), tool_result_max=4096)
    assert not any(
        isinstance(event, ProcessEvent) and event.kind == "compaction"
        for event in events
    )
    assert [event.turn_id for event in events if isinstance(event, TurnEnd)] == [
        "new-turn"
    ]


def test_pending_compaction_keeps_authoritative_abort_terminal(tmp_path):
    from cc_remote.wrapper.codex_stream import codex_translate_history

    source = tmp_path / "compact-aborted.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-aborted"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "context_compacted"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "turn_aborted", "turn_id": "turn-aborted"}},
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(str(source), tool_result_max=4096)
    compactions = [
        event for event in events
        if isinstance(event, ProcessEvent) and event.kind == "compaction"
    ]
    terminals = [event for event in events if isinstance(event, TurnEnd)]
    assert len(compactions) == 1
    assert compactions[0].turn_id == "turn-aborted"
    assert len(terminals) == 1
    assert terminals[0].turn_id == "turn-aborted"
    assert terminals[0].result.subtype == "error_during_execution"
    assert terminals[0].result.is_error is True


def test_pending_compaction_keeps_authoritative_failure_terminal(tmp_path):
    from cc_remote.wrapper.codex_stream import codex_translate_history

    source = tmp_path / "compact-failed.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-failed"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "context_compacted"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "task_failed", "turn_id": "turn-failed"}},
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(str(source), tool_result_max=4096)
    compactions = [
        event for event in events
        if isinstance(event, ProcessEvent) and event.kind == "compaction"
    ]
    terminals = [event for event in events if isinstance(event, TurnEnd)]
    assert len(compactions) == 1
    assert compactions[0].turn_id == "turn-failed"
    assert len(terminals) == 1
    assert terminals[0].turn_id == "turn-failed"
    assert terminals[0].result.subtype == "error"
    assert terminals[0].result.is_error is True


def test_compaction_owner_comparison_uses_protocol_safe_turn_id(tmp_path):
    """Provider ids are normalized before ownership comparisons.

    An unsafe raw id must not make a valid marker look as though it belongs to
    another turn and disappear from the materialized history.
    """
    from cc_remote.wrapper.codex_stream import codex_translate_history

    source = tmp_path / "compact-normalized-owner.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "unsafe turn id"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "context_compacted"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "turn_context",
         "payload": {"turn_id": "unsafe turn id"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "continue"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "unsafe turn id",
                     "last_agent_message": "done"}},
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(str(source), tool_result_max=4096)
    compaction = next(
        event for event in events
        if isinstance(event, ProcessEvent) and event.kind == "compaction"
    )
    terminal = next(event for event in events if isinstance(event, TurnEnd))
    assert compaction.turn_id is not None
    # A hashed display identity must never be exposed as a resumable fork point.
    assert terminal.turn_id is None


def test_compact_only_eof_does_not_materialize_an_empty_turn(tmp_path):
    from cc_remote.wrapper.codex_stream import codex_translate_history

    source = tmp_path / "compact-eof.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-open"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "context_compacted"}},
    ]))

    events, _ = codex_translate_history(str(source), tool_result_max=4096)
    assert not any(isinstance(event, ProcessEvent) for event in events)
    assert not any(isinstance(event, TurnEnd) for event in events)


def test_history_agent_message_borrows_paired_live_item_id(tmp_path):
    from cc_remote.wrapper.codex_stream import codex_translate_history

    source = tmp_path / "paired-agent-message.jsonl"
    clean = "working"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-1"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "inspect"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "commentary",
                     "message": clean}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "response_item",
         "payload": {"type": "message", "id": "msg-stable",
                     "role": "assistant", "phase": "commentary",
                     "content": [{
                         "type": "output_text",
                         "text": clean + "\n\n<internal metadata>",
                     }]}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-1",
                     "last_agent_message": clean}},
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(str(source), tool_result_max=4096)
    starts = [event for event in events if isinstance(event, AssistantMsgStart)]
    deltas = [event for event in events if isinstance(event, Delta)]
    ends = [event for event in events if isinstance(event, AssistantMsgEnd)]
    assert [event.message_id for event in starts] == ["msg-stable"]
    assert [(event.message_id, event.text) for event in deltas] == [
        ("msg-stable", clean)
    ]
    assert [event.message_id for event in ends] == ["msg-stable"]


def test_unpaired_history_agent_message_is_not_lost_at_snapshot_eof(tmp_path):
    from cc_remote.wrapper.codex_stream import codex_translate_history

    source = tmp_path / "unpaired-agent-message.jsonl"
    prefix = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-1"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "inspect"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "commentary",
                     "message": "visible before pair"}},
    ]
    suffix = {
        "timestamp": "2026-01-01T00:00:03Z", "type": "response_item",
        "payload": {"type": "message", "id": "msg-after-snapshot",
                    "role": "assistant", "phase": "commentary",
                    "content": [{
                        "type": "output_text", "text": "visible before pair",
                    }]},
    }
    encoded_prefix = "".join(json.dumps(row) + "\n" for row in prefix)
    # end_offset below is a byte offset into this exact \n-only content, so
    # write it verbatim rather than letting Windows expand \n to \r\n.
    source.write_text(encoded_prefix + json.dumps(suffix) + "\n", newline="")

    events, _ = codex_translate_history(
        str(source), tool_result_max=4096,
        end_offset=len(encoded_prefix.encode()),
    )
    deltas = [event for event in events if isinstance(event, Delta)]
    assert [event.text for event in deltas] == ["visible before pair"]
    assert deltas[0].message_id != "msg-after-snapshot"


def test_active_snapshot_defers_unpaired_agent_message_until_canonical_item(
        tmp_path):
    """A growing rollout must not publish a temporary id at the mirror seam."""
    from cc_remote.wrapper.codex_stream import codex_translate_history

    source = tmp_path / "growing-agent-message.jsonl"
    prefix = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-1"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "inspect"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "commentary",
                     "message": "one logical update"}},
    ]
    paired = {
        "timestamp": "2026-01-01T00:00:03Z", "type": "response_item",
        "payload": {"type": "message", "id": "msg-canonical",
                    "role": "assistant", "phase": "commentary",
                    "content": [{
                        "type": "output_text", "text": "one logical update",
                    }]},
    }
    encoded_prefix = "".join(json.dumps(row) + "\n" for row in prefix)
    source.write_text(encoded_prefix + json.dumps(paired) + "\n")

    partial, _ = codex_translate_history(
        str(source), tool_result_max=4096,
        end_offset=len(encoded_prefix.encode()),
        snapshot_in_progress=True,
    )
    assert not any(isinstance(event, Delta) for event in partial)

    complete, _ = codex_translate_history(
        str(source), tool_result_max=4096,
        end_offset=source.stat().st_size,
        snapshot_in_progress=True,
    )
    deltas = [event for event in complete if isinstance(event, Delta)]
    assert [(event.message_id, event.text) for event in deltas] == [
        ("msg-canonical", "one logical update"),
    ]


def test_authoritative_page_continuation_can_close_without_terminal(tmp_path):
    """Only the history selector can authorize a page-prefix continuation."""
    from cc_remote.wrapper.codex_stream import codex_translate_history
    source = tmp_path / "page-continuation.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "final_answer",
                     "message": "tail from an oversized turn"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "next"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "final_answer",
                     "message": "next answer"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "next-turn"}},
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(
        str(source), tool_result_max=4096,
        source_continuation="authoritative_page",
    )
    turn_ends = [e for e in events if type(e).__name__ == "TurnEnd"]
    assert [e.turn_id for e in turn_ends] == [None, "next-turn"]
    assert [e.result.subtype for e in turn_ends] == ["success", "success"]


def test_assistant_only_dangling_without_source_evidence_is_error(tmp_path):
    from cc_remote.wrapper.codex_stream import codex_translate_history
    source = tmp_path / "unproven-assistant-only.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "turn_context",
         "payload": {"turn_id": "background"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "final_answer",
                     "message": "partial background output"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "next"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "final_answer",
                     "message": "next answer"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "next-turn"}},
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(str(source), tool_result_max=4096)
    turn_ends = [e for e in events if type(e).__name__ == "TurnEnd"]
    assert [e.turn_id for e in turn_ends] == [None, "next-turn"]
    assert [e.result.subtype for e in turn_ends] == ["error", "success"]
    assert [e.result.is_error for e in turn_ends] == [True, False]


def test_get_history_survives_transcript_read_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no transcript")
    monkeypatch.setattr(mm, "get_session_messages", boom)

    async def go():
        m, tr = _mk_machine()
        await m._handle_get_history(SimpleNamespace(
            session_id="sX", client_id="c1", cwd=None, type="get_history"))
        # The wrapper still replies and stops the loading attempt, but a read
        # failure is not authoritative evidence that the transcript is empty.
        assert len(tr.sent) == 1 and tr.sent[0].type == "history"
        assert tr.sent[0].events == []
        assert tr.sent[0].authoritative is False
        assert tr.sent[0].error == "历史暂时不可用，请稍后重试"
    asyncio.run(go())


def test_get_history_paginates_by_turn(monkeypatch):
    """5 turns u1..u5; verify newest-page + load-older paging by turn boundary."""
    def turn(uid):
        return [UserMsg(msg_id=uid, prompt="q"),
                TurnEnd(result=TurnResult(subtype="success", duration_ms=0, is_error=False))]
    canned = [ev for uid in ("u1", "u2", "u3", "u4", "u5") for ev in turn(uid)]
    monkeypatch.setattr(mm, "get_session_messages", lambda sid, directory=None: ["m"])
    monkeypatch.setattr(mm, "translate_history", lambda msgs, mx, timestamps=None: [e.model_copy() for e in canned])
    monkeypatch.setattr(mm, "last_assistant_model", lambda msgs: None)

    async def go():
        m, tr = _mk_machine()

        async def fetch(before, limit):
            await m._handle_get_history(SimpleNamespace(
                session_id="sX", client_id="c1", cwd="/tmp", before=before, limit=limit))
            h = tr.sent[-1]
            return h, [e["msg_id"] for e in h.events if e["type"] == "user_msg"]

        # newest 2 turns
        h, uids = await fetch(None, 2)
        assert uids == ["u4", "u5"] and h.has_more is True
        assert h.oldest_id == "u4" and h.newest_id == "u5" and h.before is None
        # older page before u4
        h, uids = await fetch("u4", 2)
        assert uids == ["u2", "u3"] and h.has_more is True and h.before == "u4"
        # oldest page — u1 only, no more
        h, uids = await fetch("u2", 2)
        assert uids == ["u1"] and h.has_more is False and h.oldest_id == "u1"
    asyncio.run(go())


def test_translate_history_stamps_real_timestamps():
    """UserMsg gets the ask-time; TurnEnd gets the turn's last message time
    (answer-done) — NOT 'now'. This fixes the 'every message shows now' clock bug."""
    from cc_remote.wrapper.stream import translate_history
    msgs = [
        SimpleNamespace(uuid="u1", type="user",
                        message={"role": "user", "content": "hi"}),
        SimpleNamespace(uuid="a1", type="assistant",
                        message={"role": "assistant", "content": [{"type": "text", "text": "hello"}]}),
    ]
    events = translate_history(msgs, 10000, timestamps={"u1": 1000.0, "a1": 1005.0})
    um = next(e for e in events if e.type == "user_msg")
    te = next(e for e in events if e.type == "turn_end")
    assert um.ts == 1000.0        # question time
    assert te.ts == 1005.0        # answer-done = last (assistant) message time
    assert te.result.duration_ms == 5000
    # missing timestamps must not crash (falls back to the _Base default)
    missing = translate_history(msgs, 10000)
    assert any(e.type == "user_msg" for e in missing)
    assert next(e for e in missing if e.type == "turn_end").result.duration_ms == 0

    backwards = translate_history(
        msgs, 10000, timestamps={"u1": 1005.0, "a1": 1000.0})
    assert next(
        e for e in backwards if e.type == "turn_end"
    ).result.duration_ms == 0


def test_translate_history_duration_spans_tool_results():
    """Tool-result user rows belong to the existing human turn and must not
    restart its elapsed clock."""
    from cc_remote.wrapper.stream import translate_history
    msgs = [
        SimpleNamespace(
            uuid="u1", type="user",
            message={"role": "user", "content": "inspect"}),
        SimpleNamespace(
            uuid="a1", type="assistant",
            message={"role": "assistant", "content": [{
                "type": "tool_use", "id": "tool-1", "name": "Read",
                "input": {"file_path": "/tmp/a"},
            }]}),
        SimpleNamespace(
            uuid="r1", type="user",
            message={"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "tool-1",
                "content": "contents",
            }]}),
        SimpleNamespace(
            uuid="a2", type="assistant",
            message={"role": "assistant", "content": [{
                "type": "text", "text": "done",
            }]}),
    ]
    events = translate_history(
        msgs, 10000, timestamps={
            "u1": 1000.0, "a1": 1002.0, "r1": 1004.0, "a2": 1007.0,
        })

    ends = [e for e in events if e.type == "turn_end"]
    assert len(ends) == 1
    assert ends[0].result.duration_ms == 7000


def test_task_notification_history_is_structured_only_with_raw_origin_evidence(
        monkeypatch, tmp_path):
    notification = """<task-notification>
<task-id>agent-task-1</task-id>
<tool-use-id>call-agent-1</tool-use-id>
<status>completed</status>
<summary>Agent \"code survey\" finished</summary>
<result>{}</result>
<usage><subagent_tokens>123</subagent_tokens><tool_uses>7</tool_uses><duration_ms>4500</duration_ms></usage>
</task-notification>""".format("very large private result " * 2000)
    path = tmp_path / "session.jsonl"
    rows = [
        {
            "type": "queue-operation", "operation": "enqueue",
            "content": notification,
        },
        {
            "type": "user", "uuid": "notification-row",
            "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": notification},
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    monkeypatch.setattr(
        "cc_remote.wrapper.stream.transcript_path", lambda _sid: str(path))

    metadata = transcript_internal_user_events("session")
    messages = [
        SimpleNamespace(
            uuid="human-turn", type="user",
            message={"role": "user", "content": "research"}),
        SimpleNamespace(
            uuid="assistant-row", type="assistant",
            message={"role": "assistant", "content": [{
                "type": "tool_use", "id": "call-agent-1", "name": "Agent",
                "input": {"description": "code survey"},
            }]}),
        SimpleNamespace(
            uuid="notification-row", type="user",
            message={"role": "user", "content": notification}),
    ]
    events = translate_history(
        messages, 10_000, internal_user_events=metadata)

    assert not any(
        isinstance(event, UserMsg) and "task-notification" in event.prompt
        for event in events)
    process = next(
        event for event in events
        if event.type == "process" and event.item_id == "agent:call-agent-1")
    assert process.kind == "agent" and process.phase == "end"
    assert process.status == "succeeded"
    assert process.parent_id == "call-agent-1"
    assert process.turn_id == "human-turn"
    assert process.title == 'Agent "code survey" finished'
    assert process.progress == "7 次工具调用 · 4.5s"
    assert process.output is None

    # Content shape alone is not authority. A human pasting the same XML stays
    # a visible user message when the raw transcript origin does not mark it as
    # an internal task notification.
    visible = translate_history([SimpleNamespace(
        uuid="human-paste", type="user",
        message={"role": "user", "content": notification},
    )], 10_000, internal_user_events=metadata)
    assert next(event for event in visible if isinstance(event, UserMsg)).prompt \
        == notification


def test_history_hides_cancelled_command_placeholders_without_hiding_real_text():
    user_id = "11111111-1111-4111-8111-111111111111"
    answer_id = "22222222-2222-4222-8222-222222222222"
    synthetic_ids = (
        "33333333-3333-4333-8333-333333333333",
        "44444444-4444-4444-8444-444444444444",
    )
    messages = [
        SimpleNamespace(
            uuid=user_id,
            type="user",
            message={"role": "user", "content": "hello"},
        ),
        SimpleNamespace(
            uuid=answer_id,
            type="assistant",
            message={
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "real answer"}],
            },
        ),
        *[
            SimpleNamespace(
                uuid=uid,
                type="assistant",
                message={
                    "role": "assistant",
                    "model": "<synthetic>",
                    "content": [{
                        "type": "text",
                        "text": "No response requested.",
                    }],
                },
            )
            for uid in synthetic_ids
        ],
    ]
    timestamps = {
        user_id: 1000.0,
        answer_id: 1005.0,
        synthetic_ids[0]: 1010.0,
        synthetic_ids[1]: 1015.0,
    }

    events = translate_history(messages, 10_000, timestamps=timestamps)
    deltas = [event.text for event in events if isinstance(event, Delta)]
    assert deltas == ["real answer"]
    terminal = next(event for event in events if isinstance(event, TurnEnd))
    assert terminal.turn_id == answer_id
    assert terminal.ts == 1005.0
    assert last_assistant_model(messages) == "claude-sonnet-5"

    real_same_text = SimpleNamespace(
        uuid="55555555-5555-4555-8555-555555555555",
        type="assistant",
        message={
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": "No response requested."}],
        },
    )
    visible = translate_history([messages[0], real_same_text], 10_000)
    assert any(
        isinstance(event, Delta) and event.text == "No response requested."
        for event in visible
    )


def test_claude_history_marks_sdk_interrupt_without_a_fake_user_turn():
    prompt_id = "11111111-1111-4111-8111-111111111111"
    marker_id = "22222222-2222-4222-8222-222222222222"
    messages = [
        SimpleNamespace(
            uuid=prompt_id,
            type="user",
            message={"role": "user", "content": "hello"},
        ),
        SimpleNamespace(
            uuid=marker_id,
            type="user",
            message={"role": "user", "content": [{
                "type": "text",
                "text": "[Request interrupted by user]",
            }]},
        ),
    ]

    events = translate_history(
        messages,
        10_000,
        timestamps={prompt_id: 1000.0, marker_id: 1005.0},
    )

    assert [
        event.prompt for event in events if isinstance(event, UserMsg)
    ] == ["hello"]
    terminal = next(event for event in events if isinstance(event, TurnEnd))
    assert terminal.result.subtype == "interrupted"
    assert terminal.result.is_error is False
    assert terminal.ts == 1005.0


def test_claude_history_keeps_synthetic_api_error_but_marks_turn_failed():
    prompt_id = "33333333-3333-4333-8333-333333333333"
    error_id = "44444444-4444-4444-8444-444444444444"
    text = "API Error: 529 Overloaded. This is a server-side issue."
    messages = [
        SimpleNamespace(
            uuid=prompt_id,
            type="user",
            message={"role": "user", "content": "inspect"},
        ),
        SimpleNamespace(
            uuid=error_id,
            type="assistant",
            message={
                "role": "assistant",
                "model": "<synthetic>",
                "stop_reason": "stop_sequence",
                "content": [{"type": "text", "text": text}],
            },
        ),
    ]

    events = translate_history(
        messages,
        10_000,
        timestamps={prompt_id: 1000.0, error_id: 1002.0},
    )

    assert any(
        isinstance(event, Delta) and event.text == text
        for event in events
    )
    terminal = next(event for event in events if isinstance(event, TurnEnd))
    assert terminal.result.subtype == "error"
    assert terminal.result.is_error is True
    assert last_assistant_model(messages) is None


def test_live_claude_turn_end_uses_last_assistant_transcript_uuid():
    """Tools can split one turn across several assistant transcript records.

    The branch point is the final transcript UUID, never the API message id.
    """
    first_uuid = "11111111-1111-4111-8111-111111111111"
    final_uuid = "22222222-2222-4222-8222-222222222222"
    translator = StreamTranslator(10_000)

    translator.feed(AssistantMessage(
        content=[ToolUseBlock(id="tool-1", name="Read", input={"path": "x"})],
        model="claude-test",
        message_id="api-message-id",
        uuid=first_uuid,
    ))
    translator.feed(UserMessage(
        content=[ToolResultBlock(tool_use_id="tool-1", content="ok")],
        uuid="33333333-3333-4333-8333-333333333333",
    ))
    translator.feed(AssistantMessage(
        content=[TextBlock(text="done")],
        model="claude-test",
        message_id="same-or-new-api-id",
        uuid=final_uuid,
    ))
    events = translator.feed(ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=9,
        is_error=False,
        num_turns=2,
        session_id="session-1",
    ))

    terminal = next(event for event in events if isinstance(event, TurnEnd))
    assert terminal.turn_id == final_uuid
    assert terminal.turn_id not in {"api-message-id", "same-or-new-api-id"}


def test_live_claude_turn_end_does_not_publish_non_uuid_fallback():
    translator = StreamTranslator(10_000)
    translator.feed(AssistantMessage(
        content=[TextBlock(text="partial")],
        model="claude-test",
        message_id="api-message-id",
        uuid=None,
    ))
    events = translator.feed(ResultMessage(
        subtype="error_during_execution",
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=1,
        session_id="session-1",
    ))

    assert next(event for event in events if isinstance(event, TurnEnd)).turn_id is None


def test_claude_history_turn_end_uses_final_assistant_uuid_after_tools():
    first_uuid = "44444444-4444-4444-8444-444444444444"
    final_uuid = "55555555-5555-4555-8555-555555555555"
    messages = [
        SimpleNamespace(
            uuid="66666666-6666-4666-8666-666666666666",
            type="user",
            message={"role": "user", "content": "run it"},
        ),
        SimpleNamespace(
            uuid=first_uuid,
            type="assistant",
            message={"role": "assistant", "content": [{
                "type": "tool_use", "id": "tool-1", "name": "Read",
                "input": {"path": "x"},
            }]},
        ),
        SimpleNamespace(
            uuid="77777777-7777-4777-8777-777777777777",
            type="user",
            message={"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "tool-1", "content": "ok",
            }]},
        ),
        SimpleNamespace(
            uuid=final_uuid,
            type="assistant",
            message={"role": "assistant", "content": [{
                "type": "text", "text": "done",
            }]},
        ),
    ]

    terminals = [
        event for event in translate_history(messages, 10_000)
        if isinstance(event, TurnEnd)
    ]
    assert len(terminals) == 1
    assert terminals[0].turn_id == final_uuid


def test_claude_history_never_uses_repaired_legacy_id_as_fork_point():
    messages = [
        SimpleNamespace(
            uuid="88888888-8888-4888-8888-888888888888",
            type="user",
            message={"role": "user", "content": "hello"},
        ),
        SimpleNamespace(
            uuid="",
            type="assistant",
            message={"role": "assistant", "content": [{
                "type": "text", "text": "answer",
            }]},
        ),
    ]

    terminal = next(
        event for event in translate_history(messages, 10_000)
        if isinstance(event, TurnEnd)
    )
    assert terminal.turn_id is None


def test_legacy_history_repairs_missing_message_and_tool_ids_stably():
    messages = [
        SimpleNamespace(
            type="user", uuid="",
            message={"role": "user", "content": "hello"},
        ),
        SimpleNamespace(
            type="assistant", uuid="",
            message={"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "input": {"path": "x"}},
                {"type": "text", "text": "done"},
            ]},
        ),
        SimpleNamespace(
            type="user", uuid="user-tool-result",
            message={"role": "user", "content": [
                {"type": "tool_result", "content": "ok"},
            ]},
        ),
    ]

    first = translate_history(messages, 10_000)
    second = translate_history(messages, 10_000)
    first_ids = [
        getattr(event, name)
        for event in first
        for name in ("msg_id", "message_id", "tool_use_id")
        if getattr(event, name, None)
    ]
    second_ids = [
        getattr(event, name)
        for event in second
        for name in ("msg_id", "message_id", "tool_use_id")
        if getattr(event, name, None)
    ]
    assert first_ids and first_ids == second_ids
    assert all(identifier and len(identifier) <= 128 for identifier in first_ids)
