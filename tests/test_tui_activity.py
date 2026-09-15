"""Progress-scoped tool groups and independent outer activity folds."""

import json

import pytest

from cc_remote.tui_app import Composer, Transcript, WorkspaceApp, location
from cc_remote.tui_state import (
    Block,
    MAX_BLOCK_CHARS,
    SessionView,
    TRUNCATED,
    WorkspaceState,
)
from tests.test_tui_send_jumps import client


def progress(view, identity, text):
    view.event(
        dict(
            type="delta",
            message_id=identity,
            channel="commentary",
            text=text,
            turn_id="t",
        )
    )


def tool(view, identity, name="Bash", inputs=None):
    view.event(
        dict(
            type="tool_use",
            tool_use_id=identity,
            tool=name,
            input=inputs or {"command": "private payload"},
            turn_id="t",
        )
    )
    view.event(
        dict(
            type="tool_result",
            tool_use_id=identity,
            content="private output",
            turn_id="t",
        )
    )


def seed(view):
    view.event(dict(type="user_msg", msg_id="t", prompt="task"))
    progress(view, "p1", "First progress")
    tool(view, "a")
    tool(view, "b", "Edit", {"file_path": "a.py"})
    tool(view, "c", "Write", {"file_path": "b.py"})
    progress(view, "p2", "Second progress")
    tool(view, "d")


def test_progress_groups_default_closed_and_outer_stays_open_on_completion():
    view = SessionView()
    seed(view)
    text, starts = view.render()
    groups = [b for _, b in starts if b.role == "tool_group"]
    assert len(groups) == 2 and not any(g.expanded for g in groups)
    assert "3 个工具调用 · 修改 2 个文件" in text
    assert "1 个工具调用" in text
    assert "private payload" not in text and "private output" not in text
    assert text.index("First progress") < text.index("3 个工具调用")
    assert text.index("3 个工具调用") < text.index("Second progress")
    assert text.index("Second progress") < text.index("1 个工具调用")
    groups[0].expanded = True
    view.event(
        dict(
            type="delta",
            message_id="answer",
            channel="final",
            text="Final",
            turn_id="t",
        )
    )
    view.event(dict(type="turn_end", turn_id="t", result={}))
    text, _ = view.render()
    assert view.local_details["t"].expanded and groups[0].expanded
    assert "First progress" in text and "private payload" in text
    assert not groups[1].expanded


def test_folds_keep_independent_state_and_resolve_hidden_child_anchors():
    view = SessionView()
    seed(view)
    view.render()
    inner = view.tool_groups["tools:a"]
    inner.expanded = True
    text, starts = view.render()
    assert "private output" in text
    outer = view.local_details["t"]
    outer.expanded = False
    text, starts = view.render()
    assert "First progress" not in text
    assert view.resolve(("b", 12), starts, len(text)) == next(
        start for start, b in starts if b.id == outer.id
    )
    tool(view, "late")
    view.render()
    assert inner.expanded and not outer.expanded
    outer.expanded = True
    text, starts = view.render()
    assert inner.expanded and "private output" in text
    inner.expanded = False
    text, starts = view.render()
    assert view.resolve(("a", 12), starts, len(text)) == next(
        start for start, b in starts if b.id == inner.id
    )


def test_file_change_paths_are_deduplicated_and_hooks_are_not_tool_calls():
    view = SessionView()
    view.event(dict(type="user_msg", msg_id="t", prompt="task"))
    for identity in ("f1", "f2"):
        view.event(
            dict(
                type="process",
                item_id=identity,
                turn_id="t",
                kind="file_change",
                tool="apply_patch",
                status="succeeded",
                input={"file_paths": ["a", "b", "a"]},
            )
        )
    view.event(
        dict(
            type="process",
            item_id="h",
            turn_id="t",
            kind="hook",
            status="succeeded",
            title="postToolUse",
        )
    )
    text, _ = view.render()
    assert "2 个工具调用 · 修改 2 个文件 · 1 项活动" in text


def test_partial_canonical_detail_keeps_progress_order_and_deduplicates():
    state = WorkspaceState()
    view = state.view("s")
    seed(view)
    view.revision = "r"
    state.event(
        dict(
            type="turn_detail",
            session_id="s",
            revision="r",
            turn_id="t",
            events=[
                dict(
                    type="tool_use",
                    tool_use_id="a",
                    tool="Bash",
                    input={"command": "hydrated"},
                )
            ],
        )
    )
    text, starts = view.render()
    assert text.index("First progress") < text.index("3 个工具调用")
    view.tool_groups["tools:a"].expanded = True
    text, starts = view.render()
    assert text.count("hydrated") == 1
    assert sum(b.id == "a" for _, b in starts) == 1
    state.event(
        dict(type="history_invalidated", session_id="s", revision="new")
    )
    assert not view.detail_blocks and not view.tool_groups
    assert not view.local_details and not view.render()[0]


@pytest.mark.asyncio
async def test_cold_history_loads_only_on_demand_and_keeps_final_outside():
    c = client()
    view = c.workspace.view("s")
    view.blocks = [
        Block("earlier", "user", "Previous turn", "older"),
        Block("answer", "assistant", "Short final", "t", "final"),
        Block("detail:t", "detail", turn="t"),
    ]
    view.revision = "r"
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        await pilot.pause()
        assert not c._outbox
        start = next(i for i, b in app.starts if b.role == "tool_group")
        assert start < reader.text.index("Short final")
        reader.move_cursor(location(reader.text, start))
        await pilot.press("enter")
        frames = [json.loads(raw) for raw, _ in c._outbox.values()]
        assert len(frames) == 1 and frames[0]["type"] == "get_turn_detail"
        c.workspace.event(
            dict(
                type="turn_detail",
                session_id="s",
                turn_id="t",
                revision="r",
                events=[
                    dict(
                        type="delta",
                        message_id="p",
                        channel="commentary",
                        text="Historical progress",
                    ),
                    dict(
                        type="tool_use",
                        tool_use_id="a",
                        tool="Bash",
                        input={"command": "historical command"},
                    ),
                    dict(
                        type="delta",
                        message_id="answer",
                        channel="final",
                        text="Complete final",
                    ),
                ],
            )
        )
        app.paint()
        await pilot.pause()
        assert "Historical progress" in reader.text
        assert "historical command" not in reader.text
        assert "Short final" not in reader.text
        assert reader.text.count("Complete final") == 1
        assert view.local_details["t"].expanded
        assert not view.tool_groups["tools:a"].expanded
        text, starts = view.render()
        assert view.resolve(("tools:history:t", 0), starts, len(text)) == next(
            i for i, b in starts if b.id == "detail:t"
        )
        view.local_details["t"].expanded = False
        text, _ = view.render()
        assert "Historical progress" not in text
        assert "Complete final" in text


def test_detail_cache_cannot_resurrect_evicted_or_btw_rebuilt_turns():
    view = SessionView()
    seed(view)
    view.detail_blocks["t"] = [
        Block("old", "assistant", "Stale", "t", "commentary"),
    ]
    view.render()
    view.blocks.clear()
    assert not view.render()[0] and not view.detail_blocks
    seed(view)
    view.render()
    view.event(dict(type="replay_start", sid="btw-test", rebuild=True))
    assert not view.render()[0] and not view.tool_groups
    assert not view.local_details


def test_native_turn_alias_keeps_outer_and_inner_fold_choices():
    view = SessionView()
    seed(view)
    for block in view.blocks:
        block.seq = 2
    view.render()
    view.local_details["t"].expanded = False
    view.collapsed_details.add("t")
    view.tool_groups["tools:a"].expanded = True
    view.history(
        dict(
            type="history",
            revision="r",
            live_seq=1,
            turns=[
                dict(
                    id="native",
                    clientMsgId="t",
                    prompt="task",
                    blocks=[],
                )
            ],
        )
    )
    view.render()
    assert not view.local_details["native"].expanded
    assert "native" in view.collapsed_details
    assert view.tool_groups["tools:a"].expanded
    assert view.tool_groups["tools:a"].turn == "native"
    assert "t" not in view.local_details
    text, starts = view.render()
    assert view.resolve(("detail:t", 0), starts, len(text)) == next(
        i for i, b in starts if b.id == "detail:native"
    )


def test_typed_detail_cache_is_bounded_and_does_not_overwrite_newer_live():
    state = WorkspaceState()
    view = state.view("s")
    view.revision = "r"
    view.put(Block("answer", "assistant", "Live final", "t", "final", seq=99))
    state.event(
        dict(
            type="turn_detail",
            session_id="s",
            turn_id="t",
            revision="r",
            events=[
                dict(
                    type="delta",
                    message_id="answer",
                    channel="final",
                    text="Stale final",
                    seq=1,
                ),
                dict(
                    type="delta",
                    message_id="p",
                    channel="commentary",
                    text="x" * (MAX_BLOCK_CHARS * 2),
                ),
            ],
        )
    )
    size = sum(
        len(b.text) + len(json.dumps(b.data, ensure_ascii=False))
        for b in view.detail_blocks["t"]
    )
    assert size <= MAX_BLOCK_CHARS
    text, _ = view.render()
    assert "Live final" in text and "Stale final" not in text
    assert TRUNCATED.strip() in text
    state.event(
        dict(
            type="turn_detail",
            session_id="s",
            turn_id="t",
            revision="r",
            reset_required=True,
        )
    )
    assert "t" not in view.detail_blocks


def test_tool_summary_does_not_retain_oversized_inputs():
    view = SessionView(active_turn="t")
    tool(
        view,
        "huge",
        inputs={
            "first": "x" * MAX_BLOCK_CHARS,
            "second": "y" * MAX_BLOCK_CHARS,
        },
    )
    assert view.blocks[0].data["input"] == {}
    assert len(view.blocks[0].text) <= MAX_BLOCK_CHARS
    assert "1 个工具调用" in view.render()[0]


def test_async_question_is_not_hidden_with_its_turn_activity():
    view = SessionView()
    seed(view)
    view.put(
        Block(
            "question",
            "assistant",
            "Where?",
            "t",
            "commentary",
            data={"delivery": "async", "questions": [{"title": "Where?"}]},
        )
    )
    view.render()
    view.local_details["t"].expanded = False
    text, _ = view.render()
    assert "First progress" not in text and "Where?" in text


@pytest.mark.asyncio
async def test_two_enter_layers_close_from_any_tool_body_and_final():
    c = client()
    view = c.workspace.view("s")
    view.blocks.clear()
    seed(view)
    view.event(
        dict(
            type="delta",
            message_id="answer",
            channel="final",
            text="Final answer",
            turn_id="t",
        )
    )
    view.event(dict(type="turn_end", turn_id="t", result={}))
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        await pilot.pause()
        start = next(i for i, b in app.starts if b.id == "tools:a")
        reader.move_cursor(location(reader.text, start))
        await pilot.press("enter")
        app.paint()
        await pilot.pause()
        assert "private payload" in reader.text
        reader.move_cursor(
            location(reader.text, reader.text.index("private output"))
        )
        await pilot.press("enter")
        app.paint()
        await pilot.pause()
        assert "private payload" not in reader.text
        assert "First progress" in reader.text
        reader.move_cursor(
            location(reader.text, reader.text.index("Final answer"))
        )
        await pilot.press("enter")
        app.paint()
        await pilot.pause()
        assert "First progress" not in reader.text
        assert "Final answer" in reader.text
        await pilot.press("enter")
        app.paint()
        await pilot.pause()
        assert "First progress" in reader.text
        assert "private output" not in reader.text
        assert not c._outbox


@pytest.mark.asyncio
@pytest.mark.parametrize("bottom", [True, False])
async def test_streaming_collapsed_groups_preserves_every_reader_frame(
    monkeypatch,
    bottom,
):
    c = client()
    view = c.workspace.view("s")
    seed(view)
    app = WorkspaceApp(c, connect=False)
    async with app.run_test(size=(70, 26)) as pilot:
        await pilot.pause()
        reader = app.query_one(Transcript)
        reader.move_cursor((10, 0))
        app.query_one(Composer).focus()
        reader.scroll_to(
            y=reader.max_scroll_y if bottom else 12,
            immediate=True,
            animate=False,
        )
        await pilot.pause()
        frames = []
        original = app._display

        def frame(screen, renderable):
            if renderable is not None:
                frames.append((reader.scroll_y, reader.max_scroll_y))
            original(screen, renderable)

        monkeypatch.setattr(app, "_display", frame)
        for n in range(3):
            tool(view, f"new-{n}")
            progress(view, f"p-{n}", "More progress\n" * 5)
            app.paint()
            await pilot.pause()
        assert frames
        assert all(y == (end if bottom else 12) for y, end in frames)
        assert "private output" not in reader.text
