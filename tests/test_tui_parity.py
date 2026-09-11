"""Web/TUI public-event parity and keyboard/control regressions (no model)."""

import json

import pytest
from textual.widgets import Input, Static, TextArea

from cc_remote import protocol as p
from cc_remote.tui_actions import ACTIONS, build_action, defaults
from cc_remote.tui_app import (
    Composer,
    Transcript,
    WorkspaceApp,
    WorkspaceClient,
    seed_demo,
)
from cc_remote.tui_attachments import read_attachment
from cc_remote.tui_panels import (
    ActionForm,
    DetailPanel,
    PanelReader,
    QuestionDialog,
    QueueEdit,
    QueuePanel,
)
from cc_remote.tui_presentation import (
    SessionPresentation,
    TurnDisplay,
    merge_rate_window,
    quota_windows,
    tokens,
)
from cc_remote.tui_state import SessionView, WorkspaceState


def event(view, event_type, **kw):
    view.event({"type": event_type, **kw})


def app_client():
    client = WorkspaceClient("ws://localhost:8765/ws", "", "", "codex", "s")
    client.workspace.catalog = {
        "s": {"session_id": "s", "cwd": "/repo", "engine": "codex"}
    }
    client.workspace.view("s").write_state = "writable"
    return WorkspaceApp(client, connect=False), client


def test_native_turn_binding_late_terminal_never_closes_new_turn():
    view = SessionView()
    event(view, "user_msg", msg_id="one", prompt="first", ts=100, seq=1)
    event(view, "turn_binding", msg_id="one", turn_id="native-one")
    event(view, "user_msg", msg_id="two", prompt="second", ts=120, seq=3)
    event(view, "turn_binding", msg_id="two", turn_id="native-two")
    event(
        view,
        "turn_end",
        turn_id="native-one",
        ts=130,
        result={"subtype": "success", "is_error": False, "duration_ms": 2500},
    )
    assert view.presentation.turns["native-one"].label() == "completed · 2s"
    assert view.presentation.turns["native-two"].status == "running"
    assert view.presentation.active == "native-two"


def test_claude_terminal_assistant_id_closes_unbound_user_owner():
    view = SessionView()
    event(view, "user_msg", msg_id="user", prompt="hi", ts=100)
    event(
        view,
        "turn_end",
        turn_id="assistant-id",
        checkpoint_id="user",
        ts=110,
        result={
            "subtype": "error_during_execution",
            "is_error": True,
            "duration_ms": 10000,
        },
    )
    assert view.presentation.turns["user"].status == "interrupted"
    assert "assistant-id" not in view.presentation.turns


def test_current_activity_thinking_tool_exit_and_background_are_distinct():
    view = SessionView()
    event(view, "user_msg", msg_id="u", prompt="test", ts=100)
    event(
        view, "delta", message_id="a", channel="thinking", text="summary\nmore"
    )
    assert view.presentation.turns["u"].activity == "Thinking"
    event(
        view,
        "process",
        item_id="cmd",
        kind="command",
        title="Run pytest",
        phase="begin",
        status="running",
    )
    event(
        view,
        "process",
        item_id="cmd",
        kind="command",
        title="Run pytest",
        phase="delta",
        append_to="output",
        delta="passed ",
    )
    event(
        view,
        "process",
        item_id="cmd",
        kind="command",
        title="Run pytest",
        phase="delta",
        append_to="output",
        delta="42",
    )
    block = next(b for b in view.blocks if b.id == "cmd")
    assert block.data["output"] == "passed 42"
    event(view, "tool_use", tool_use_id="t", tool="exec", input={"cmd": "true"})
    event(
        view,
        "tool_result",
        tool_use_id="t",
        content="ok",
        is_error=False,
        duration_ms=1200,
        exit_code=0,
    )
    rendered = view.render()[0]
    assert "2 个工具调用 · 1 项活动" in rendered
    assert "Thinking" not in rendered
    next(iter(view.tool_groups.values())).expanded = True
    rendered = view.render()[0]
    assert "Thinking" in rendered and "[Enter: expand]" not in rendered
    assert "succeeded · 1s · exit 0" in rendered
    event(
        view,
        "turn_end",
        result={"subtype": "success", "is_error": False, "duration_ms": 1200},
    )
    event(
        view,
        "process",
        item_id="bg",
        kind="agent",
        title="Background agent",
        phase="begin",
        status="running",
        background=True,
    )
    assert view.presentation.turns["u"].status == "completed"
    event(
        view,
        "background_process_sync",
        items=[{"item_id": "bg", "title": "Agent"}],
    )
    event(view, "background_process_sync", items=[])
    assert view.presentation.background == []


def test_goal_new_user_boundary_and_plan_progress_do_not_stick_to_old_goal():
    view = SessionView()
    event(
        view,
        "goal_state",
        goal_id="g",
        goal={"status": "complete", "objective": "old"},
    )
    event(view, "user_msg", msg_id="new", prompt="new work")
    event(
        view,
        "turn_plan",
        item_id="p",
        plan=[{"step": "new step", "status": "inProgress"}],
    )
    assert view.presentation.visible_goal() is None
    assert view.presentation.progress_label().startswith("Plan 0/1")
    event(
        view,
        "turn_plan",
        item_id="p",
        plan=[{"step": "new step", "status": "completed"}],
    )
    assert "Plan 1/1" in view.presentation.progress_label()
    event(view, "user_msg", msg_id="next", prompt="more")
    assert view.presentation.plan is None
    event(
        view,
        "goal_state",
        goal_id="g2",
        goal={"status": "active", "objective": "replacement"},
    )
    assert view.presentation.visible_goal()["objective"] == "replacement"


def test_unfinished_plan_survives_clarification_but_not_next_turn_after_terminal():
    view = SessionView()
    event(view, "user_msg", msg_id="u", prompt="work")
    event(
        view,
        "turn_plan",
        item_id="p",
        plan=[{"step": "step", "status": "inProgress"}],
    )
    event(
        view,
        "turn_steered",
        msg_id="clarify",
        turn_id="u",
        prompt="clarification",
    )
    assert view.presentation.plan is not None
    event(
        view,
        "turn_end",
        turn_id="u",
        result={"subtype": "success", "duration_ms": 1},
    )
    assert "not updated" in view.presentation.progress_label()
    event(view, "user_msg", msg_id="next", prompt="continue")
    assert view.presentation.plan is None
    event(
        view,
        "turn_plan",
        item_id="p2",
        plan=[{"step": "step", "status": "inProgress"}],
    )
    assert view.presentation.plan["turn_id"] == "next"


def summary(**kw):
    return {
        "type": "history",
        "revision": "r",
        "generation": "g",
        "detail": "summary",
        "build_seq": 1,
        "turns": [],
        **kw,
    }


def test_cold_history_restores_latest_plan_tool_thinking_and_duration():
    view = SessionView()
    turns = []
    for i in (1, 2):
        turns.append(
            {
                "id": str(i),
                "prompt": "work",
                "ts": i * 100000,
                "done": i == 1,
                "durationMs": 9000 if i == 1 else None,
                "blocks": [
                    {
                        "kind": "process",
                        "item_id": f"plan{i}",
                        "processKind": "plan",
                        "plan": [{"step": f"step{i}", "status": "inProgress"}],
                    }
                ],
            }
        )
    view.history(summary(turns=turns))
    assert view.presentation.plan["turn_id"] == "2"
    assert view.presentation.turns["1"].duration_ms == 9000
    event(
        view,
        "turn_plan",
        item_id="plan2",
        turn_id="2",
        seq=100,
        plan=[{"step": "step2", "status": "completed"}],
    )
    view.history(summary(turns=turns, live_seq=90, build_seq=2))
    assert view.presentation.plan["plan"][0]["status"] == "completed"


def test_exact_terminal_fences_do_not_close_unrelated_rows():
    view = SessionView()
    view.history(
        summary(
            turns=[
                {"id": "u", "forkPointId": "native", "prompt": "one"},
                {"id": "other", "prompt": "two"},
            ],
            terminal_fences=[
                {
                    "turn_id": "native",
                    "status": "completed",
                    "duration_ms": 12000,
                }
            ],
        )
    )
    assert view.presentation.turns["u"].status == "completed"
    assert view.presentation.turns["other"].status == "running"


def test_sparse_quota_and_unknown_context_never_fabricate_zero():
    view = SessionView()
    event(view, "context_report", percentage=0, available=False)
    assert view.presentation.usage_label() == "Context unavailable"
    event(
        view, "rate_limit_update", primary={"used_percent": 2, "resets_at": 100}
    )
    event(view, "rate_limit_update", primary={"resets_at": 101})
    event(view, "status_report", rate_limits=[{"primary": {"used_percent": 0}}])
    assert view.presentation.rates["default"]["primary"] == {
        "used_percent": 2,
        "resets_at": 101,
    }


def test_usage_human_units_and_relative_daily_scale():
    assert tokens(2651000000) == "26.51亿"
    assert tokens(1000000000000) == "1兆"
    p = SessionPresentation(
        status={
            "usage": {
                "lifetime_tokens": 20000,
                "daily_usage_buckets": [
                    {"start_date": "2026-09-01", "tokens": 100},
                    {"start_date": "2026-09-02", "tokens": 200},
                ],
            }
        }
    )
    content = p.panel("Usage / Context")
    assert "2万" in content and "2026-09-02 [██████████]" in content


def test_completion_revision_and_goal_hide_follow_session_rekey():
    state = WorkspaceState()
    state.event(
        {
            "type": "completion_state",
            "sid": "tmp",
            "completion_id": "c",
            "unread": True,
            "revision": 2,
        }
    )
    state.event(
        {
            "type": "completion_state",
            "sid": "tmp",
            "unread": False,
            "revision": 1,
        }
    )
    state.event(
        {
            "type": "goal_state",
            "sid": "tmp",
            "goal_id": "g",
            "dismissed": True,
            "goal": {"status": "complete"},
        }
    )
    state.event(
        {"type": "session_rekey", "old_key": "tmp", "session_id": "real"}
    )
    assert state.view("real").presentation.completion["unread"]
    assert state.view("real").presentation.visible_goal() is None


@pytest.mark.asyncio
async def test_late_goal_read_cannot_replace_new_broadcast_goal():
    _, client = app_client()
    await client._send(p.GetGoal(sid="s", cmd_id="read-old"))
    client._on_event(
        {
            "type": "goal_state",
            "sid": "s",
            "goal_id": "new",
            "goal": {"status": "active"},
        }
    )
    client._on_event(
        {
            "type": "goal_state",
            "sid": "s",
            "request_id": "read-old",
            "goal_id": "old",
            "goal": {"status": "complete"},
        }
    )
    assert client.workspace.view("s").presentation.goal_id == "new"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "Goal / Plan",
        "Usage / Context",
        "Background",
        "Status",
        "Notices",
        "Reports",
        "Web handoff",
    ],
)
async def test_detail_panels_keep_reader_draft_and_session_scope(name):
    app, client = app_client()
    client.demo = True
    async with app.run_test() as pilot:
        await pilot.press("ctrl+j", "i", "x", "escape", "ctrl+k")
        position = app.query_one(Transcript).cursor_location
        await app.action_panel(name)
        assert isinstance(app.screen, DetailPanel)
        client.workspace.view("other").presentation.goal = {
            "objective": "NOT THIS SESSION"
        }
        await pilot.pause()
        assert "NOT THIS SESSION" not in app.screen.query_one(TextArea).text
        await pilot.press("ctrl+s", "ctrl+e", "escape")
        assert app.query_one(Composer).text == "x"
        assert app.query_one(Transcript).cursor_location == position
        assert not client._outbox


@pytest.mark.asyncio
async def test_named_action_submits_once_and_keeps_session_target():
    app, client = app_client()
    async with app.run_test() as pilot:
        app.push_screen(ActionForm(client, "s", "rename_session"))
        await pilot.pause()
        from cc_remote.tui_fields import ParameterFields
        form = app.screen.query_one(ParameterFields)
        editor = next(row[-1] for row in form.rows if row[0] == "title")
        editor.load_text("renamed")
        await pilot.press("enter")
        await pilot.pause()
        messages = [json.loads(raw) for raw, _ in client._outbox.values()]
        assert (
            messages[-1]["type"] == "rename_session"
            and messages[-1]["title"] == "renamed"
        )
        await pilot.press("enter")
        await pilot.pause()
        assert len(client._outbox) == 1


def test_protocol_actions_are_validated_not_arbitrary_rpc_or_routing():
    assert {
        "fork_session",
        "set_goal",
        "manage_engine_hook",
        "create_work_project",
    } <= ACTIONS.keys()
    with pytest.raises(ValueError, match="Transport"):
        build_action("set_model", '{"sid":"other","model":"x"}', "s", "client")
    with pytest.raises(ValueError):
        build_action("set_effort", '{"effort":"bogus"}', "s", "client")
    with pytest.raises(ValueError, match="target is pinned"):
        build_action("delete_session", '{"session_id":"other"}', "s", "client")
    msg = build_action(
        "fork_session", '{"session_id":"s","last_turn_id":"t"}', "s", "client"
    )
    assert msg.request_id == msg.cmd_id and msg.sid == "s"
    for name in ACTIONS:
        values = defaults(
            name, "s", "codex", {"cwd": "/repo"}, SessionPresentation()
        )
        assert "sid" not in values and "cmd_id" not in values


@pytest.mark.asyncio
async def test_secret_question_is_masked_scoped_and_never_put_in_draft():
    app, client = app_client()
    ask = {
        "type": "ask_user",
        "sid": "s",
        "ask_id": "ask",
        "question": "Password?",
        "secret": True,
        "allow_text": True,
        "options": [],
    }
    client._handle(ask)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+t")
        assert isinstance(app.screen, QuestionDialog)
        input_ = app.screen.query_one(Input)
        assert input_.password
        input_.value = "secret answer"
        await pilot.press("enter")
        assert app.query_one(Composer).text == ""
        assert "secret answer" not in client.workspace.view("s").render()[0]
        sent = json.loads(next(iter(client._outbox.values()))[0])
        assert sent["type"] == "answer_question" and sent["sid"] == "s"


@pytest.mark.asyncio
async def test_question_renders_wire_option_descriptions():
    app, client = app_client()
    ask = p.AskUser(
        sid="s", ask_id="ask", question="Choose",
        options=[{"label": "Safe", "ds": "Keeps existing files"},
                 {"label": "Replace", "ds": "Overwrites existing files"}],
    ).model_dump()
    client._handle(ask)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+t")
        assert "Keeps existing files" in app.screen.query_one(PanelReader).text


def test_attachment_validation_reuses_shared_limits(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("notes")
    attachment = read_attachment(str(f))
    assert attachment["content"]["filename"] == "notes.txt"
    with pytest.raises(ValueError):
        read_attachment(str(f), image=True)
    with pytest.raises(ValueError):
        read_attachment(str(tmp_path))


@pytest.mark.asyncio
async def test_codex_running_uses_steer_and_queue_remains_server_owned():
    _, client = app_client()
    view = client.workspace.view("s")
    view.state = "running"
    assert await client.submit("clarify")
    assert await client.submit("next", queue=True)
    frames = [json.loads(raw) for raw, _ in client._outbox.values()]
    assert frames[0]["type"] == "steer"
    assert frames[1]["type"] == "query" and frames[1]["delivery"] == "queue"
    assert not view.blocks and not view.queue


@pytest.mark.asyncio
async def test_queue_edit_uses_private_full_prompt_not_preview():
    app, client = app_client()
    async with app.run_test() as pilot:
        app.push_screen(QueueEdit(client, "s", "queued"))
        await pilot.pause()
        screen = app.screen
        client._on_event(
            {
                "type": "queued_query_detail",
                "sid": "s",
                "msg_id": "queued",
                "request_id": screen.request_id,
                "prompt": "full prompt beyond preview",
            }
        )
        await pilot.pause(0.2)
        assert screen.query_one(TextArea).text == "full prompt beyond preview"
        screen.query_one(TextArea).load_text("edited")
        await pilot.press("enter")
        await pilot.pause()
        frames = [json.loads(raw) for raw, _ in client._outbox.values()]
        assert (
            frames[-1]["type"] == "update_queued_query"
            and frames[-1]["prompt"] == "edited"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "rejected", "error"])
async def test_queue_edit_waits_for_exact_server_result(outcome):
    app, client = app_client()
    async with app.run_test() as pilot:
        app.push_screen(QueueEdit(client, "s", "queued"))
        await pilot.pause()
        screen = app.screen
        client._on_event({
            "type": "queued_query_detail", "sid": "s", "msg_id": "queued",
            "request_id": screen.request_id, "prompt": "original",
        })
        screen.receive_detail()
        screen.query_one(TextArea).load_text("edited draft")
        await screen.action_submit()
        request = screen.update_request_id
        assert request
        result = screen.query_one("#queue-result", Static)
        assert "not yet confirmed" in str(result.render())
        count = len(client._outbox)
        await screen.action_submit()
        assert len(client._outbox) == count
        response = {
            "type": "queued_query_updated", "sid": "s", "msg_id": "queued",
            "request_id": request, "updated": True, "to": client.client_id,
        }
        for mismatch in (
            {"sid": "another"}, {"msg_id": "another"},
            {"request_id": "another"}, {"to": "another"},
        ):
            client._handle(response | mismatch)
            screen.receive_detail()
            assert screen.update_request_id == request
        client._handle({
            "type": "command_ack", "client_id": client.client_id,
            "cmd_id": request,
        })
        screen.receive_detail()
        assert screen.update_request_id == request
        if outcome == "rejected":
            response.update(updated=False, error="Already started")
        elif outcome == "error":
            response = {
                "type": "error", "sid": "s", "request_id": request,
                "to": client.client_id, "message": "Already started",
            }
        client._handle(response)
        screen.receive_detail()
        assert screen.update_request_id is None
        expected = ("saved by server" if outcome == "accepted"
                    else "rejected: Already started")
        assert expected in str(result.render())
        assert screen.query_one(TextArea).text == "edited draft"
        assert not client.queue_updates and not client.queue_update_results


@pytest.mark.asyncio
async def test_queue_edit_closing_discards_pending_confirmation():
    app, client = app_client()
    async with app.run_test() as pilot:
        app.push_screen(QueueEdit(client, "s", "queued"))
        await pilot.pause()
        screen = app.screen
        screen.loaded = True
        await screen.action_submit()
        request = screen.update_request_id
        assert request in client.queue_updates
        app.pop_screen()
        await pilot.pause()
        client._handle({
            "type": "queued_query_updated", "sid": "s", "msg_id": "queued",
            "request_id": request, "updated": True, "to": client.client_id,
        })
        assert not client.queue_updates and not client.queue_update_results


@pytest.mark.asyncio
async def test_queue_edit_fast_reply_is_not_overwritten_by_send(monkeypatch):
    app, client = app_client()
    async with app.run_test() as pilot:
        app.push_screen(QueueEdit(client, "s", "queued"))
        await pilot.pause()
        screen = app.screen
        screen.loaded = True

        async def immediate_reply(command):
            client._handle({
                "type": "queued_query_updated", "sid": "s",
                "msg_id": "queued", "request_id": command.cmd_id,
                "updated": False, "error": "Already started",
            })
            screen.receive_detail()
            return True

        monkeypatch.setattr(client, "_send", immediate_reply)
        await screen.action_submit()
        result = screen.query_one("#queue-result", Static)
        assert "rejected: Already started" in str(result.render())


@pytest.mark.asyncio
async def test_demo_has_rich_content_and_no_writes_from_panels():
    app, client = app_client()
    seed_demo(client)
    text = client.workspace.view("demo-review").render()[0]
    assert "unbound switches" not in text
    assert "Ctrl+j focuses the draft" in text
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.press("space", "g")
        assert "Check regressions" in app.screen.query_one(TextArea).text
        await pilot.press("escape", "space", "l")
        assert isinstance(app.screen, QueuePanel)
        await pilot.press("escape")
        assert not client._outbox


def test_missing_timestamp_is_explicit_not_fabricated():
    assert TurnDisplay().label() == "Processing · duration unavailable"


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"used_percent": 0}, {"used_percent": 2, "resets_at": 100}),
        (
            {"resets_at": 101, "used_percent": None},
            {"used_percent": 2, "resets_at": 101},
        ),
        ({"resets_at": 200}, {"resets_at": 200}),
        (
            {"resets_at": 200, "used_percent": 0},
            {"resets_at": 200, "used_percent": 0},
        ),
        (
            {"resets_at": 10, "used_percent": 90},
            {"used_percent": 2, "resets_at": 100},
        ),
    ],
)
def test_rate_window_matches_web_period_and_monotonic_rules(update, expected):
    assert (
        merge_rate_window({"used_percent": 2, "resets_at": 100}, update)
        == expected
    )


@pytest.mark.asyncio
async def test_fresh_status_can_replace_quota_after_account_change_but_old_read_cannot():
    _, c = app_client()
    c._on_event(
        {
            "type": "rate_limit_update",
            "sid": "s",
            "primary": {"used_percent": 100},
        }
    )
    await c._send(p.GetStatus(sid="s", cmd_id="old"))
    c._on_event(
        {
            "type": "rate_limit_update",
            "sid": "s",
            "reached_type": "",
            "primary": {"used_percent": 2},
        }
    )
    c._on_event(
        {
            "type": "status_report",
            "sid": "s",
            "request_id": "old",
            "rate_limits": [{"primary": {"used_percent": 0}}],
        }
    )
    assert (
        c.workspace.view("s").presentation.rates["default"]["primary"][
            "used_percent"
        ]
        == 100
    )
    await c._send(p.GetStatus(sid="s", cmd_id="fresh"))
    c._on_event(
        {
            "type": "status_report",
            "sid": "s",
            "request_id": "fresh",
            "rate_limits": [{"primary": {"used_percent": 2}}],
        }
    )
    assert (
        c.workspace.view("s").presentation.rates["default"]["primary"][
            "used_percent"
        ]
        == 2
    )


def test_restart_generation_accepts_fresh_completion_revision_and_distrusts_old_control():
    view = SessionView(write_state="writable")
    event(view, "snapshot", generation="old", state="running")
    event(
        view, "completion_state", completion_id="old", unread=True, revision=20
    )
    event(view, "snapshot", generation="new", state="idle")
    event(
        view, "completion_state", completion_id="new", unread=False, revision=1
    )
    assert view.presentation.completion["completion_id"] == "new"
    assert view.write_state == "unknown"


def test_old_user_replay_does_not_retire_completed_goal():
    view = SessionView()
    event(
        view,
        "goal_state",
        goal_id="g",
        goal={"status": "complete", "updatedAt": 200},
    )
    event(view, "user_msg", msg_id="old", prompt="old work", ts=100)
    assert view.presentation.visible_goal() is not None


def test_all_existing_command_surfaces_are_exposed_or_owned_by_core_ui():
    from typing import get_args

    commands = {
        c.model_fields["type"].default
        for c in get_args(p.AnyMessage)
        if issubclass(c, p._Command)
    }
    core = {
        "query",
        "steer",
        "switch_session",
        "sync_btw",
        "get_history",
        "get_turn_detail",
        "answer_question",
    }
    assert commands == set(ACTIONS) | core


@pytest.mark.asyncio
async def test_work_attach_and_reconnect_preserve_space():
    _, c = app_client()
    c.workspace.catalog["work-session"] = {
        "session_id": "work-session",
        "engine": "codex",
        "space": "work",
    }
    await c._attach("work-session", "codex")
    messages = [json.loads(raw) for raw, _ in c._outbox.values()]
    assert (
        messages[0]["type"] == "switch_session"
        and messages[0]["space"] == "work"
    )
    sent = []

    async def capture(raw):
        sent.append(json.loads(raw))
        return True

    c._send_raw = capture
    await c._recovery_preamble()
    assert all(
        m["space"] == "work" for m in sent if m["type"] == "switch_session"
    )


def test_btw_snapshot_empty_replaces_side_chats_without_losing_main_session():
    state = WorkspaceState()
    state.catalog["main"] = {"summary": "main", "engine": "codex"}
    state.event(
        {
            "type": "btw_sync",
            "generation": "g",
            "revision": 1,
            "sessions": [
                {
                    "btw_sid": "btw-1",
                    "parent_sid": "main",
                    "engine": "codex",
                    "state": "idle",
                }
            ],
        }
    )
    assert "btw-1" in state.catalog
    state.event(
        {"type": "btw_sync", "generation": "g", "revision": 2, "sessions": []}
    )
    assert "btw-1" not in state.catalog and "main" in state.catalog


@pytest.mark.asyncio
async def test_skill_completion_uses_profile_cwd_cache_without_input_time_request():
    app, c = app_client()
    c.capability_cache[
        c.capability_key({"engine": "codex", "space": "code", "cwd": "/repo"})
    ] = {"items": [{"kind": "skill", "name": "review", "enabled": True}]}
    async with app.run_test() as pilot:
        await pilot.press(
            "ctrl+j", "i", "dollar_sign", "r", "ctrl+space", "enter"
        )
        assert app.query_one(Composer).text == "$review "
        assert not c._outbox


@pytest.mark.asyncio
async def test_completion_read_receipt_only_for_visible_followed_session():
    app, c = app_client()
    c.workspace.view("other").presentation.completion = {
        "completion_id": "other-c",
        "unread": True,
    }
    c.workspace.view("s").presentation.completion = {
        "completion_id": "mine",
        "unread": True,
    }
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        messages = [json.loads(raw) for raw, _ in c._outbox.values()]
        acknowledgements = [
            m for m in messages if m["type"] == "acknowledge_completion"
        ]
        assert len(acknowledgements) == 1 and acknowledgements[0]["sid"] == "s"


def test_private_responses_for_other_clients_are_never_projected():
    _, c = app_client()
    c._handle(
        {
            "type": "file_preview",
            "sid": "s",
            "to": "not-this-client",
            "content": "secret",
        }
    )
    assert not c.workspace.view("s").presentation.reports


def test_quota_selects_account_bucket_and_native_window_durations():
    rates = {
        "model": {
            "primary": {"used_percent": 100, "window_duration_mins": 300}
        },
        "codex": {
            "primary": {"used_percent": 20, "window_duration_mins": 10080},
            "secondary": {"used_percent": 2, "window_duration_mins": 300},
        },
    }
    windows = dict(quota_windows(rates, "codex", 0))
    assert windows["5h"]["used_percent"] == 2
    assert windows["Week"]["used_percent"] == 20
    assert dict(quota_windows({"model": rates["model"]}, "codex", 0)) == {
        "5h": None,
        "Week": None,
    }


def test_free_and_claude_quotas_do_not_get_mislabeled_as_paid_codex():
    rates = {"codex": {"primary": {"used_percent": 22}}}
    assert quota_windows(rates, "codex", 0) == [
        ("Overall", {"used_percent": 22})
    ]
    rates = {
        "claude-seven-day-opus": {
            "primary": {
                "used_percent": 12,
                "resets_at": 1000,
                "window_duration_mins": 10080,
            }
        }
    }
    assert (
        dict(quota_windows(rates, "claude", 999))["Opus"]["used_percent"] == 12
    )
    assert "Opus" not in dict(quota_windows(rates, "claude", 1000))


def test_cold_finished_plan_does_not_survive_a_newer_prompt():
    view = SessionView()
    view.history(
        summary(
            turns=[
                {
                    "id": "old",
                    "prompt": "old",
                    "done": True,
                    "interrupted": True,
                    "blocks": [
                        {
                            "kind": "process",
                            "item_id": "p",
                            "processKind": "plan",
                            "plan": [
                                {"step": "unfinished", "status": "inProgress"}
                            ],
                        }
                    ],
                },
                {"id": "new", "prompt": "next"},
            ]
        )
    )
    assert view.presentation.plan is None


def test_goal_resume_restores_previously_retired_same_objective():
    view = SessionView()
    event(view, "goal_state", goal_id="g", goal={"status": "complete"})
    event(view, "user_msg", msg_id="u", prompt="next")
    assert view.presentation.visible_goal() is None
    event(view, "goal_state", goal_id="g", goal={"status": "active"})
    assert view.presentation.visible_goal()["status"] == "active"


def test_rebuild_does_not_reappend_process_output_over_retained_baseline():
    _, c = app_client()
    c._handle(
        {
            "type": "process",
            "sid": "s",
            "seq": 1,
            "item_id": "p",
            "output": "once",
            "phase": "begin",
            "kind": "command",
        }
    )
    c._handle(
        {
            "type": "replay_start",
            "sid": "s",
            "rebuild": True,
            "generation": "new",
            "from_seq": 0,
            "to_seq": 2,
        }
    )
    c._handle(
        {
            "type": "process",
            "sid": "s",
            "seq": 2,
            "item_id": "p",
            "delta": "once",
            "append_to": "output",
            "phase": "delta",
            "kind": "command",
        }
    )
    assert c.workspace.view("s").blocks[0].data["output"] == "once"
    assert c.cursors["s"] == 2


@pytest.mark.asyncio
async def test_modal_actions_are_reachable_and_confirmable_without_mouse():
    app, c = app_client()
    async with app.run_test(size=(60, 22)) as pilot:
        app.push_screen(ActionForm(c, "s", "set_effort"))
        await pilot.pause()
        editor = app.screen.query_one(TextArea)
        editor.load_text('high')
        editor.focus()
        await pilot.press("enter")
        assert len(c._outbox) == 1
        await pilot.press("escape")
        assert app.query_one(Composer).text == ""


@pytest.mark.asyncio
async def test_slash_action_opens_form_without_sending_text_to_model():
    app, c = app_client()
    async with app.run_test() as pilot:
        app.query_one(Composer).load_text("/set_effort")
        await pilot.press("ctrl+j", "enter")
        assert isinstance(app.screen, ActionForm)
        assert app.screen.panel_name == "set_effort"
        assert not c._outbox


@pytest.mark.asyncio
async def test_narrow_layout_hides_empty_bars_and_keeps_transcript_visible():
    app, c = app_client()
    async with app.run_test(size=(60, 22)) as pilot:
        await pilot.pause()
        for name in (
            "#question",
            "#progress",
            "#attachments",
            "#suggestions",
            "#settings",
        ):
            assert not app.query_one(name).display
        assert app.query_one(Transcript).size.height >= 8
        await pilot.press("ctrl+j", "i", "dollar_sign")
        c.capability_cache[
            c.capability_key(
                {"engine": "codex", "space": "code", "cwd": "/repo"}
            )
        ] = {"items": [{"kind": "skill", "name": "new-skill"}]}
        app.paint()
        assert app.query_one("#suggestions").display


@pytest.mark.asyncio
async def test_archived_session_remains_searchable_in_session_picker():
    app, c = app_client()
    c.workspace.event(
        {
            "type": "session_list",
            "engine": "codex",
            "sessions": [
                {"session_id": "s", "engine": "codex", "summary": "active"},
                {
                    "session_id": "archived",
                    "engine": "codex",
                    "summary": "old",
                    "tag": "archived",
                },
            ],
        }
    )
    async with app.run_test() as pilot:
        from textual.widgets import OptionList

        assert not app.query(OptionList)
        await pilot.press("space", "e", "slash")
        app.screen.query_one(Input).value = "archived"
        await pilot.pause()
        from cc_remote.tui_tree import SessionTree

        listing = app.query_one(SessionTree)
        assert listing.cursor_node.data == ("session", "archived")


@pytest.mark.asyncio
async def test_rekeyed_queue_read_and_panel_action_keep_exact_session():
    _, c = app_client()
    c.workspace.view("tmp")
    await c._send(
        p.GetQueuedQuery(
            sid="tmp", msg_id="q", cmd_id="read", client_id=c.client_id
        )
    )
    c._handle({"type": "session_rekey", "old_key": "tmp", "session_id": "real"})
    c._handle(
        {
            "type": "queued_query_detail",
            "sid": "real",
            "msg_id": "q",
            "request_id": "read",
            "prompt": "full",
        }
    )
    assert c.queue_details["read"]["prompt"] == "full"
    await c._send(p.RenameSession(sid="tmp", session_id="tmp", title="new"))
    frame = json.loads(list(c._outbox.values())[-1][0])
    assert frame["sid"] == frame["session_id"] == "real"


@pytest.mark.asyncio
async def test_recovery_loads_other_engine_and_work_catalogs():
    _, c = app_client()
    frames = []

    async def capture(raw):
        frames.append(json.loads(raw))
        return True

    c._send_raw = capture
    await c._recovery_preamble()
    assert {
        (f["engine"], f["space"])
        for f in frames
        if f["type"] == "list_sessions"
    } == {("claude", "code"), ("claude", "work"), ("codex", "work")}
    assert not any(
        f["type"] in {"query", "steer", "new_session"} for f in frames
    )


@pytest.mark.asyncio
async def test_help_works_before_selecting_a_session():
    app, c = app_client()
    c.attached_sid = None
    async with app.run_test() as pilot:
        await pilot.press("space", "h")
        assert isinstance(app.screen, DetailPanel)
        assert "Ctrl+j" in app.screen.query_one(TextArea).text
        assert not c._outbox


@pytest.mark.parametrize(
    "kind",
    [
        "command",
        "hook",
        "agent",
        "reasoning",
        "compaction",
        "file_change",
        "mcp",
        "terminal",
        "task",
        "safety",
    ],
)
def test_native_serialized_process_frames_preserve_sparse_output(kind):
    """Use the real serializer, including null fields omitted by small fixtures."""
    _, c = app_client()
    frames = [
        p.UserMsg(sid="s", msg_id="u", prompt="work"),
        p.ProcessEvent(
            sid="s",
            item_id="p",
            kind=kind,
            phase="start",
            status="running",
            title="Activity",
            command="command",
            output="first",
        ),
        p.ProcessEvent(
            sid="s",
            item_id="p",
            kind=kind,
            phase="update",
            title="Activity",
            append_to="output",
            delta=" second",
        ),
        p.ProcessEvent(
            sid="s",
            item_id="p",
            kind=kind,
            phase="end",
            title="Activity",
            status="succeeded",
            exit_code=0,
            duration_ms=1200,
        ),
    ]
    for frame in frames:
        c._handle(json.loads(p.serialize(frame)))
    block = c.workspace.view("s").blocks[-1]
    assert block.data["output"] == "first second"
    assert block.data["command"] == "command"
    assert block.data["status"] == "succeeded"
    c.workspace.view("s").render()
    c.workspace.view("s").tool_groups["tools:p"].expanded = True
    assert "1s · exit 0" in c.workspace.view("s").render()[0]


def test_native_serialized_tool_detail_and_async_questions_keep_wire_shapes():
    view = SessionView()
    frames = [
        p.UserMsg(msg_id="u", prompt="work"),
        p.ToolUse(
            message_id="a",
            tool_use_id="tool",
            tool="exec",
            input={"cmd": "true"},
        ),
        p.ToolDelta(tool_use_id="tool", stream="output", delta="progress"),
        p.ToolResult(tool_use_id="tool", content="done", is_error=False),
        p.AssistantMsgStart(message_id="a"),
        p.Delta(message_id="a", text="answer"),
        p.AssistantMsgEnd(
            message_id="a",
            channel="final",
            delivery="async",
            questions=[
                p.AsyncQuestionSpec(title="Which?", options=["one", "two"])
            ],
        ),
        p.TurnEnd(
            result=p.TurnResult(
                subtype="success", duration_ms=1000, is_error=False
            )
        ),
    ]
    for frame in frames:
        view.event(json.loads(p.serialize(frame)))
    assert view.presentation.turns["u"].status == "completed"
    assert "Question · non-blocking" in view.render()[0]
    assert "Which?" in view.render()[0]
    assert "progress" in next(b.text for b in view.blocks if b.id == "tool")


@pytest.mark.asyncio
async def test_action_field_help_uses_shared_schema_without_changing_draft():
    app, c = app_client()
    async with app.run_test() as pilot:
        app.push_screen(ActionForm(c, "s", "set_effort"))
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert "high" in app.screen.query_one(TextArea).text
        assert "cmd_id" not in app.screen.query_one(TextArea).text
        await pilot.press("escape")
        assert isinstance(app.screen, ActionForm)
        assert not c._outbox


@pytest.mark.asyncio
async def test_work_catalog_invalidation_refreshes_work_not_default_code():
    _, c = app_client()
    c._handle(
        json.loads(
            p.serialize(p.SessionListInvalidated(engine="codex", space="work"))
        )
    )
    await c._flush_history_refreshes()
    frames = [json.loads(raw) for raw, _ in c._outbox.values()]
    lists = [
        (m["engine"], m["space"])
        for m in frames
        if m["type"] == "list_sessions"
    ]
    assert lists == [("codex", "work")]


def test_serialized_legacy_catalog_and_untitled_btw_are_normalized():
    _, c = app_client()
    c._handle(
        json.loads(
            p.serialize(
                p.SessionList(
                    engine="claude",
                    sessions=[p.SessionInfo(session_id="parent")],
                )
            )
        )
    )
    assert c.workspace.catalog["parent"]["engine"] == "claude"
    assert c.session_engines["parent"] == "claude"
    c.workspace.event(
        {
            "type": "btw_opened",
            "btw_sid": "btw-side",
            "parent_sid": "parent",
            "engine": "claude",
        }
    )
    assert c.workspace.catalog["btw-side"]["summary"] == "BTW · parent"


def test_capability_completion_cache_retains_names_with_bounded_directories():
    _, c = app_client()
    for index in range(40):
        c._on_event(
            {
                "type": "engine_capabilities",
                "sid": "s",
                "engine": "codex",
                "space": "code",
                "cwd": f"/repo/{index}",
                "items": [
                    {
                        "kind": "skill",
                        "name": "useful",
                        "description": "large" * 10000,
                    }
                ],
            }
        )
    assert len(c.capability_cache) == 32
    assert all(
        row["items"] == [{"kind": "skill", "name": "useful"}]
        for row in c.capability_cache.values()
    )


@pytest.mark.asyncio
async def test_sidless_native_catalog_replies_reach_the_requested_panel_only():
    _, c = app_client()
    c.workspace.catalog["s"].update(codex_profile_id="account-one")
    await c.refresh_panel("s", "Settings")
    c._handle(
        json.loads(
            p.serialize(
                p.Models(
                    engine="codex",
                    codex_profile_id="account-two",
                    models=[{"id": "other"}],
                )
            )
        )
    )
    assert "models" not in c.workspace.view("s").presentation.reports
    c._handle(
        json.loads(
            p.serialize(
                p.Models(
                    engine="codex",
                    codex_profile_id="account-one",
                    models=[{"id": "wanted"}],
                )
            )
        )
    )
    assert c.workspace.view("s").presentation.reports["models"]["models"] == [
        {"id": "wanted"}
    ]
    await c.prefetch_capabilities("s")
    request_id, request = next(
        (key, value)
        for key, value in c.catalog_reads.items()
        if value["type"] == "get_engine_capabilities"
    )
    c._handle(
        json.loads(
            p.serialize(
                p.EngineCapabilities(
                    engine="codex",
                    space="code",
                    cwd="/repo",
                    codex_profile_id="account-one",
                    request_id=request_id,
                    items=[
                        p.EngineCapabilityItem(
                            kind="skill", id="skill", name="my-skill"
                        )
                    ],
                )
            )
        )
    )
    assert "engine_capabilities" in c.workspace.view("s").presentation.reports
    assert (
        c.capability_cache[c.capability_key(request)]["items"][0]["name"]
        == "my-skill"
    )


def test_profile_scoped_actions_seed_the_selected_session_account():
    row = {"cwd": "/repo", "codex_profile_id": "account-one"}
    for name in (
        "manage_engine_plugin",
        "manage_engine_skill",
        "get_models",
        "get_engine_capabilities",
    ):
        assert (
            defaults(name, "s", "codex", row, SessionPresentation())[
                "codex_profile_id"
            ]
            == "account-one"
        )


@pytest.mark.asyncio
async def test_btw_rebuild_uses_its_ring_not_native_history_and_keeps_draft():
    _, c = app_client()
    c.attached_sid = "btw-side"
    view = c.workspace.view("btw-side")
    view.draft = "kept"
    view.event({"type": "user_msg", "msg_id": "old", "prompt": "old baseline"})
    frames = [
        p.ReplayStart(
            sid="btw-side",
            generation="g",
            rebuild=True,
            truncated=False,
            from_seq=0,
            to_seq=2,
        ),
        p.UserMsg(sid="btw-side", seq=1, msg_id="u", prompt="side question"),
        p.Delta(sid="btw-side", seq=2, message_id="a", text="side answer"),
        p.ReplayEnd(sid="btw-side", to_seq=2, truncated=False),
    ]
    for frame in frames:
        c._handle(json.loads(p.serialize(frame)))
    assert "old baseline" not in view.render()[0]
    assert (
        "side question" in view.render()[0]
        and "side answer" in view.render()[0]
    )
    assert view.draft == "kept"
    assert "btw-side" not in c._history_refresh_now
    assert not await c._request_history("btw-side", force=True)
    sent = []

    async def capture(raw):
        sent.append(json.loads(raw))
        return True

    c._send_raw = capture
    await c._recovery_preamble()
    sync = next(m for m in sent if m["type"] == "sync_btw")
    assert sync["cursor"] == 2 and sync["generation"] == "g"
    assert not any(
        m["type"] in {"switch_session", "get_history", "new_session"}
        for m in sent
    )


def test_btw_catalog_removal_disables_input_without_erasing_readable_text():
    state = WorkspaceState()
    state.event(
        {
            "type": "btw_opened",
            "btw_sid": "btw-side",
            "parent_sid": "s",
            "engine": "codex",
        }
    )
    view = state.view("btw-side")
    view.write_state = "writable"
    view.draft = "kept"
    state.event(
        {
            "type": "user_msg",
            "sid": "btw-side",
            "msg_id": "u",
            "prompt": "kept history",
        }
    )
    state.event(
        {"type": "btw_sync", "generation": "g", "revision": 1, "sessions": []}
    )
    assert view.write_state == "unavailable"
    assert view.draft == "kept" and "kept history" in view.render()[0]


def test_message_actions_seed_native_fork_and_checkpoint_not_ui_row_ids():
    view = SessionView()
    view.history(
        summary(
            turns=[
                {
                    "id": "ui-old",
                    "forkPointId": "native-fork",
                    "checkpointId": "native-checkpoint",
                    "prompt": "old",
                    "done": True,
                },
                {"id": "ui-new", "prompt": "new"},
            ]
        )
    )
    view.presentation.selected_turn = "ui-old"
    fork = defaults("fork_session", "s", "claude", {}, view.presentation)
    rewind = defaults("rollback_session", "s", "claude", {}, view.presentation)
    assert fork["last_turn_id"] == "native-fork"
    assert rewind["checkpoint_id"] == "native-checkpoint"
    rewind = defaults("rollback_session", "s", "codex", {}, view.presentation)
    assert rewind["checkpoint_id"] is None


def test_epoch_change_resets_cold_receipts_before_the_new_catalog():
    state = WorkspaceState()
    state.event(
        {
            "type": "snapshot",
            "sid": "active",
            "generation": "old",
            "state": "idle",
        }
    )
    state.event(
        {
            "type": "completion_state",
            "sid": "cold",
            "completion_id": "old",
            "unread": True,
            "revision": 100,
        }
    )
    state.event(
        {
            "type": "snapshot",
            "sid": "active",
            "generation": "new",
            "state": "idle",
        }
    )
    state.event(
        {
            "type": "session_list",
            "engine": "codex",
            "sessions": [
                {
                    "session_id": "cold",
                    "completion_id": "new",
                    "completion_unread": False,
                    "completion_revision": 1,
                }
            ],
        }
    )
    assert state.view("cold").presentation.completion["completion_id"] == "new"
    assert not state.view("cold").presentation.completion["unread"]
