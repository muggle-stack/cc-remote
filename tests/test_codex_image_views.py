"""Viewed-image recovery from native nested tool calls; no live engine needed."""
import asyncio
import base64
import io
import json
from types import SimpleNamespace

from PIL import Image
import pytest

from cc_remote.wrapper import codex_stream
from cc_remote.wrapper.history_store import HistoryIndexStore, HistorySourceFingerprint
from cc_remote.wrapper.machine import _merge_codex_history_image_views
from tests.test_codex_detail_pages import _long_rollout, _write as _write_rows
from tests.test_multisession import _mk_ctx, _mk_machine


def _image(color):
    output = io.BytesIO()
    Image.new("RGB", (64, 32), color).save(output, "PNG")
    raw = output.getvalue()
    return raw, {"type": "input_image", "image_url":
                 "data:image/png;base64," + base64.b64encode(raw).decode()}


def _event(payload):
    return {"type": "event_msg", "timestamp": "2026-11-16T06:30:41Z", "payload": payload}


def _response(payload):
    return {"type": "response_item", "payload": payload}


def _batch(call="batch-1", turn="native-1"):
    return [
        _response({"type": "custom_tool_call", "call_id": call, "name": "exec",
                   "input": "throw new Error('history must not execute JavaScript')",
                   "internal_chat_message_metadata_passthrough": {"turn_id": turn}}),
        *[_event({"type": "item_completed", "turn_id": turn, "item": {
            "type": "ImageView", "id": f"{call}-view-{index}",
            "path": f"file:///missing/chart-{index}.png",
        }}) for index in range(2)],
        _response({"type": "custom_tool_call_output", "call_id": call,
                   "internal_chat_message_metadata_passthrough": {"turn_id": turn},
                   "output": [{"type": "input_text", "text": "Script completed"},
                              _image("blue")[1], _image("red")[1]]}),
    ]


def _write(path, body):
    rows = [_event({"type": "task_started", "turn_id": "native-1"}),
            _event({"type": "user_message", "message": "inspect"}), *body,
            _event({"type": "task_complete", "turn_id": "native-1"})]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _official():
    return [
        {"type": "user_msg", "msg_id": "user-1", "prompt": "inspect"},
        *[{"type": "process", "item_id": f"batch-1-view-{index}",
           "kind": "server_tool", "phase": "end", "status": "succeeded",
           "turn_id": "native-1", "tool": "view_image", "title": "查看图片",
           "input": {"file_path": f"file:///missing/chart-{index}.png"}}
          for index in range(2)],
        {"type": "turn_end", "turn_id": "native-1", "result": {"subtype": "success"}},
    ]


def test_nested_image_outputs_keep_original_bytes_without_guessing_path_order(tmp_path):
    path = tmp_path / "rollout.jsonl"
    batch = _batch()
    batch.insert(3, batch[2])  # A repeated native completion is not another image.
    _write(path, batch + _batch("batch-2"))
    views = codex_stream.codex_history_image_views(str(path), "native-1")
    assert len(views) == 4
    assert [v.data for v in views] == [_image("blue")[0], _image("red")[0]] * 2
    assert all(v.source_complete and v.source_offset is not None for v in views)
    assert all(v.event.input.get("file_path") is None for v in views)
    assert len({v.event.input["history_image"]["image_id"] for v in views}) == 4
    merged = _merge_codex_history_image_views(_official(), views[:2])
    images = [row for row in merged if row.get("tool") == "view_image"]
    assert len(images) == 2
    assert all("history_image" in row["input"] for row in images)
    assert _merge_codex_history_image_views(merged, views[:2]) == merged
    assert "base64" not in json.dumps(merged)
    assert "/missing/" not in json.dumps(merged)


@pytest.mark.parametrize("fault", ["wrong_turn", "wrong_output_turn", "wrong_call",
                                  "missing_image", "invalid_image", "parallel_call",
                                  "steered", "over_budget"])
def test_nested_images_reject_ambiguous_or_unbounded_associations(tmp_path, monkeypatch, fault):
    batch = _batch()
    if fault == "wrong_turn":
        batch = _batch(turn="other-native")
    elif fault == "wrong_output_turn":
        batch[-1]["payload"]["internal_chat_message_metadata_passthrough"]["turn_id"] = "other"
    elif fault == "wrong_call":
        batch[-1]["payload"]["call_id"] = "other-call"
    elif fault == "missing_image":
        batch[-1]["payload"]["output"].pop()
    elif fault == "invalid_image":
        batch[-1]["payload"]["output"][-1]["image_url"] = "data:image/png;base64,invalid"
    elif fault == "parallel_call":
        batch.insert(1, _response({"type": "custom_tool_call", "call_id": "parallel",
                                  "name": "exec"}))
    elif fault == "steered":
        batch.insert(1, _event({"type": "user_message", "message": "new instruction"}))
    elif fault == "over_budget":
        monkeypatch.setattr(codex_stream, "_MAX_HISTORY_IMAGE_BYTES_PER_SEGMENT", 1)
    path = tmp_path / "rollout.jsonl"
    _write(path, batch)
    assert codex_stream.codex_history_image_views(str(path), "native-1") == ()


def test_nested_image_batches_stay_in_their_native_user_segment(tmp_path):
    path = tmp_path / "rollout.jsonl"
    _write(path, [*_batch(), _event({"type": "user_message", "message": "next"}),
                  *_batch("second"), _event({"type": "task_started", "turn_id": "other"}),
                  *_batch("outside", "other")])
    for segment, call in [(0, "batch-1"), (1, "second")]:
        views = codex_stream.codex_history_image_views(str(path), "native-1", segment_index=segment)
        assert len(views) == 2
        assert all(view.call_id.startswith(call) for view in views)


@pytest.mark.parametrize("indexed", [False, True])
def test_nested_images_survive_cached_detail_reload_and_asset_eviction(tmp_path, indexed):
    path = tmp_path / "rollout.jsonl"
    _write(path, _batch())
    official = _official()

    class History:
        async def turn_events(self, *_):
            return None if indexed else official

        def summary_events(self, *_):
            return None

        def rollout_fallback(self, *_):
            return SimpleNamespace(native_turn_id="native-1", segment_index=0)

    async def run():
        for restart in range(2):
            machine, _ = _mk_machine()
            machine._history_index = HistoryIndexStore(tmp_path / "index")
            machine._codex_history = History()
            machine._codex_rollout_for_wire = lambda _: str(path)
            ctx = _mk_ctx("sid", "sid")
            ctx.engine = "codex"
            machine.sessions[ctx.key] = ctx
            if indexed:
                machine._history_index.put_turn_details(
                    "sid", "codex", HistorySourceFingerprint.capture(path), official)
            revision = machine._history_revision("sid")
            for _ in range(2):
                detail = await machine._handle_get_turn_detail(SimpleNamespace(
                    session_id="sid", turn_id="user-1", before=None, limit=192,
                    revision=revision, client_id="browser"))
                assert detail.error is None
                images = [e for e in detail.events if e.get("tool") == "view_image"]
                assert len(images) == 2
                assert all("history_image" in e["input"] for e in images)
            for index, event in enumerate(images):
                image_id = event["input"]["history_image"]["image_id"]
                if restart:
                    machine._history_index.invalidate_session("sid")
                for variant in ("thumbnail", "full"):
                    image = await machine._handle_get_history_image(SimpleNamespace(
                        session_id="sid", turn_id="user-1", revision=revision,
                        image_id=image_id, variant=variant, request_id=variant, client_id="browser"))
                    assert image.error is None
                    if variant == "full":
                        assert base64.b64decode(image.data) == _image(["blue", "red"][index])[0]

    asyncio.run(run())


def test_nested_image_long_turn_paging_keeps_exact_cursor_and_lazy_bytes(tmp_path):
    path = tmp_path / "long-rollout.jsonl"
    _long_rollout(path)
    # Image output is near the oldest part of the selected user segment.
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[5:5] = _batch(turn="native-long")
    _write_rows(path, rows)

    async def run():
        machine, _ = _mk_machine()
        machine.cfg.codex_history_window_max_bytes = 1024 * 1024
        machine._history_index = HistoryIndexStore(tmp_path / "index")
        machine._codex_history = None
        machine._codex_rollout_for_wire = lambda _: str(path)
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        revision = machine._history_revision("sid")
        args = dict(session_id="sid", turn_id="user-long", before=None, limit=3,
                    revision=revision, client_id="browser")
        images = []
        seen_cursors = set()
        while True:
            page = await machine._handle_get_turn_detail(SimpleNamespace(**args))
            assert page.error is None
            images += [e for e in page.events if e.get("tool") == "view_image"]
            if not page.has_more:
                break
            assert page.oldest_cursor not in seen_cursors
            seen_cursors.add(page.oldest_cursor)
            args["before"] = page.oldest_cursor
        assert len(images) == 2
        originals = {view.event.input["history_image"]["image_id"]: view.data for view in
                     codex_stream.codex_history_image_views(str(path), "native-long")}
        for index, event in enumerate(images):
            image = await machine._handle_get_history_image(SimpleNamespace(
                session_id="sid", turn_id="user-long", revision=revision,
                image_id=event["input"]["history_image"]["image_id"], variant="full",
                request_id=str(index), client_id="browser"))
            assert image.error is None
            assert base64.b64decode(image.data) == originals[image.image_id]

    asyncio.run(run())
