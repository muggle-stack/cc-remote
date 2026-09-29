"""Read-only long-turn paging: source windows, display pages, and cursor scope."""
import json
import asyncio
from types import SimpleNamespace

import pytest

from cc_remote.wrapper.codex_detail_pages import CodexDetailPages, CodexDetailCursorExpired
from cc_remote.wrapper.machine import _turn_detail_page
from tests.test_multisession import _mk_ctx, _mk_machine


def _event(payload):
    return {"type": "event_msg", "payload": payload}


def _user(native, visible, prompt):
    return [
        _event({"type": "task_started", "turn_id": native}),
        _event({"type": "item_completed", "turn_id": native, "item": {
            "type": "UserMessage", "id": visible,
            "content": [{"type": "text", "text": prompt}],
        }}),
    ]


def _message(index, native="native-long"):
    return _event({"type": "item_completed", "turn_id": native, "item": {
        "type": "AgentMessage", "id": f"progress-{index}", "phase": "commentary",
        "content": [{"type": "Text", "text": f"Public progress {index}"}],
    }})


def _write(path, rows, mode="w"):
    with path.open(mode) as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def _semantic(page):
    return ([{k: v for k, v in event.items() if k != "ts"} for event in page[0]], *page[1:])


def _long_rollout(path, *, newer=False):
    rows = _user("native-old", "user-old", "Earlier question")
    rows += [_event({"type": "task_complete", "turn_id": "native-old"})]
    rows += _user("native-long", "user-long", "The long question")
    for index in range(40):
        rows += [_message(index), {"type": "compacted", "payload": {
            "message": "PRIVATE SUMMARY", "replacement_history": ["x" * 90_000],
        }}]
    if newer:
        rows += [_event({"type": "task_complete", "turn_id": "native-long"})]
        rows += _user("native-new", "user-new", "Later question") + [_message("new", "native-new")]
    _write(path, rows)


def _read(pager, path, before=None, **overrides):
    args = dict(before=before, limit=3, window_bytes=1024 * 1024,
                max_bytes=512 * 1024, tool_result_max=65536, paginate=_turn_detail_page)
    args.update(overrides)
    return pager.read(str(path), "iris@session", "user-long", "revision-1", **args)


@pytest.mark.parametrize("newer", [False, True])
def test_long_turn_pages_recover_every_public_message_and_navigate_back(tmp_path, newer):
    path = tmp_path / "rollout.jsonl"
    _long_rollout(path, newer=newer)
    pager = CodexDetailPages()
    page = _read(pager, path)
    ids = []
    seen = set()
    pages = []
    before = None
    while page is not None:
        events, has_more, older, has_newer, newer_cursor = page
        pages.append((before, page))
        assert all(e.get("prompt") == "The long question"
                   for e in events if e["type"] == "user_msg")
        assert not any("PRIVATE SUMMARY" in e.get("text", "") for e in events)
        assert any(e["type"] == "turn_binding" and e["turn_id"] == "native-long" for e in events)
        ids += [e["message_id"] for e in events if e["type"] == "delta"]
        if not has_more:
            break
        assert older is not None and older not in seen
        seen.add(older)
        before = older
        page = _read(pager, path, before)
    assert set(ids) == {f"progress-{i}" for i in range(40)}
    assert len(ids) == 40
    assert len(pages) > 10  # both byte windows and display-group pages were crossed
    # Follow the exact reverse links back to the newest frozen page.
    for _before, expected in reversed(pages[:-1]):
        assert page[3] is True
        page = _read(pager, path, page[4])
        assert _semantic(page) == _semantic(expected)


def test_long_turn_cursor_survives_append_but_rejects_changed_scope_and_rollback(tmp_path):
    path = tmp_path / "rollout.jsonl"
    _long_rollout(path)
    pager = CodexDetailPages()
    newest = _read(pager, path)
    cursor = newest[2]
    original = _read(pager, path, cursor)
    _write(path, [_message("appended")], "a")
    assert _semantic(_read(pager, path, cursor)) == _semantic(original)
    assert any(e.get("message_id") == "progress-appended" for e in _read(pager, path)[0])
    for sid, turn, revision in [
        ("other@session", "user-long", "revision-1"),
        ("iris@session", "user-old", "revision-1"),
        ("iris@session", "user-long", "revision-2"),
    ]:
        with pytest.raises(CodexDetailCursorExpired):
            pager.read(str(path), sid, turn, revision, before=cursor, limit=3,
                       window_bytes=1024 * 1024, max_bytes=512 * 1024,
                       tool_result_max=65536, paginate=_turn_detail_page)
    with pytest.raises(CodexDetailCursorExpired):
        _read(pager, path, cursor + "0")
    with pytest.raises(CodexDetailCursorExpired):
        _read(pager, path, cursor, limit=4)
    path.write_text("{}\n")
    with pytest.raises(CodexDetailCursorExpired):
        _read(pager, path, cursor)


def test_long_turn_pager_does_not_claim_short_turns_or_unknown_ids(tmp_path):
    path = tmp_path / "rollout.jsonl"
    _write(path, _user("native-long", "user-long", "short"))
    assert _read(CodexDetailPages(), path) is None
    _long_rollout(path, newer=True)
    pager = CodexDetailPages()
    assert pager.read(str(path), "iris@session", "user-new", "revision-1",
                      before=None, limit=3, window_bytes=1024 * 1024,
                      max_bytes=512 * 1024, tool_result_max=65536,
                      paginate=_turn_detail_page) is None


def test_detail_segment_fence_is_frozen_with_its_source_snapshot(tmp_path):
    path = tmp_path / "frozen-boundary.jsonl"
    _long_rollout(path)
    pager = CodexDetailPages()
    active = _read(pager, path)
    assert not any(e["type"] == "turn_end" for e in active[0])
    # A steer appended after a snapshot must not retroactively close that
    # snapshot's active EOF when its cursors are revisited.
    next_user = _user("native-long", "steer", "new instruction")[1]
    next_user["timestamp"] = "2026-09-27T17:20:00Z"
    _write(path, [next_user], "a")
    older = _read(pager, path, active[2])
    returned = _read(pager, path, older[4])
    assert _semantic(returned) == _semantic(active)
    fresh = _read(CodexDetailPages(), path)
    terminal, = [e for e in fresh[0] if e["type"] == "turn_end"]
    assert terminal["result"]["subtype"] == "steered"
    assert terminal["turn_id"] is None and not terminal["result"]["is_error"]


def test_long_turn_handler_pages_without_an_engine_and_resets_expired_cursor(tmp_path):
    path = tmp_path / "rollout.jsonl"
    _long_rollout(path)

    async def run():
        machine, transport = _mk_machine()
        machine.cfg.codex_history_window_max_bytes = 1024 * 1024
        ctx = _mk_ctx("long-session", "long-session")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        machine._codex_rollout_for_wire = lambda _sid: str(path)
        machine._codex_history = None  # no app-server or model API is available
        revision = machine._history_revision(ctx.key)
        args = dict(session_id=ctx.key, turn_id="user-long", revision=revision,
                    before=None, client_id="browser", limit=3)
        detail = await machine._handle_get_turn_detail(SimpleNamespace(**args))
        assert detail.authoritative and detail.has_more and detail.to == "browser"
        assert any(e.get("message_id") for e in detail.events if e["type"] == "delta")
        args["before"] = detail.oldest_cursor
        older = await machine._handle_get_turn_detail(SimpleNamespace(**args))
        assert older.authoritative and older.has_newer
        machine._codex_detail_pages = CodexDetailPages()  # cursor cache eviction/restart
        expired = await machine._handle_get_turn_detail(SimpleNamespace(**args))
        assert expired.reset_required and not expired.authoritative and not expired.events
        assert len(transport.sent) == 3

    asyncio.run(run())


def test_tool_call_and_result_across_source_windows_keep_the_same_native_id(tmp_path):
    path = tmp_path / "rollout.jsonl"
    rows = _user("native-long", "user-long", "The long question")
    rows += [{"type": "response_item", "payload": {"type": "function_call",
        "call_id": "seam-tool", "name": "exec_command", "arguments": '{"cmd":"ls"}'}}]
    rows += [{"type": "compacted", "payload": {"message": "x" * 50_000}}] * 30
    rows += [{"type": "response_item", "payload": {"type": "function_call_output",
        "call_id": "seam-tool", "output": "Result on the other side of the window"}}]
    _write(path, rows)
    pager = CodexDetailPages()
    newest = _read(pager, path)
    assert any(e["type"] == "tool_result" and e["tool_use_id"] == "seam-tool"
               for e in newest[0])
    page = newest
    while page[1]:
        page = _read(pager, path, page[2])
    assert any(e["type"] == "tool_use" and e["tool_use_id"] == "seam-tool"
               and e["category"] == "command" and e["input"] == {"command": "ls"}
               for e in page[0])
