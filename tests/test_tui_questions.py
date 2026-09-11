"""Async questions are not final turns or interrupt requests."""

import json

import pytest
from textual.widgets import Static

from cc_remote.tui_app import Composer, Transcript, WorkspaceApp
from cc_remote.tui_keys import KeyConfig
from cc_remote.tui_modal import ModalEditor, PickerList
from cc_remote.tui_panels import AsyncQuestionDialog
from cc_remote.tui_questions import pending_async, supplemental_answer_prompt
from cc_remote.tui_state import Block, SessionView
from tests.test_tui_send_jumps import client


def ask(view, identity="question", options=None):
    view.event(dict(
        type="assistant_msg_end", message_id=identity,
        turn_id="active", channel="final", text="Which directory?",
        delivery="async",
        questions=[dict(title="Which directory?", options=options)],
    ))


def setup():
    c = client()
    v = c.workspace.view("s")
    v.event(dict(type="user_msg", msg_id="active", prompt="work"))
    v.state = "running"
    v.put(Block("progress", "assistant", "Working step\n" * 100,
                "active", "commentary"))
    return c, v


def frames(c):
    return [json.loads(raw) for raw, _ in c._outbox.values()]


def test_async_question_preserves_running_progress_until_real_terminal():
    _, v = setup()
    # The phase can arrive before the delivery metadata; neither is a terminal.
    v.event(dict(type="delta", message_id="question", channel="final",
                 text="Which directory?"))
    assert "Working step" in v.render()[0]
    ask(v)
    assert v.presentation.turns["active"].status == "running"
    assert "Working step" in v.render()[0]
    assert "Question · non-blocking" in v.render()[0]
    assert '"options"' not in v.render()[0]
    v.event(dict(type="turn_end", turn_id="active", result={}))
    assert "Working step" in v.render()[0]  # Outer activity stays open.
    assert "Which directory?" in v.render()[0]


def test_history_restores_async_metadata_without_json_or_false_final():
    v = SessionView()
    v.history(dict(type="history", session_id="s", revision="r", turns=[dict(
        id="active", prompt="work", done=False, blocks=[dict(
            kind="text", message_id="q", channel="final", delivery="async",
            text="Which?", questions=[dict(title="Which?", options=["a", "b"])],
        )],
    )]))
    assert [b.id for b in pending_async(v)] == ["q"]
    text = v.render()[0]
    assert "1. a" in text and "2. b" in text
    assert "Assistant · final" not in text and '"options"' not in text
    v.event(dict(type="turn_steered", msg_id="other-client", turn_id="active",
                 prompt="use a"))
    assert not pending_async(v)


@pytest.mark.asyncio
@pytest.mark.parametrize("bottom", [False, True])
async def test_question_arrival_and_answer_frames_preserve_viewport(
    monkeypatch, bottom,
):
    c, v = setup()
    app = WorkspaceApp(c, connect=False)
    async with app.run_test(size=(80, 28)) as pilot:
        await pilot.pause()
        r = app.query_one(Transcript)
        # Keep cursor in an old message, including while the draft is focused.
        r.move_cursor((10, 0))
        app.query_one(Composer).focus()
        r.scroll_to(y=r.max_scroll_y if bottom else 12,
                    animate=False, immediate=True)
        await pilot.pause()
        observed = []
        original_display = app._display

        def display(screen, renderable):
            if renderable is not None and len(app.screen_stack) == 1:
                observed.append((r.scroll_y, r.max_scroll_y))
            original_display(screen, renderable)

        monkeypatch.setattr(app, "_display", display)
        ask(v)
        app.paint()
        await pilot.pause()
        assert "Working step" in r.text
        assert observed
        assert all(y == (end if bottom else 12) for y, end in observed)
        observed.clear()
        app.action_answer()
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, AsyncQuestionDialog)
        dialog.query_one("#answer", ModalEditor).load_text("use a")
        await pilot.press("enter")
        await pilot.pause()
        v.event(dict(type="turn_steered", msg_id=frames(c)[0]["msg_id"],
                     turn_id="active", prompt="use a"))
        v.put(Block("next", "assistant", "Continuing\n" * 10,
                    "active", "commentary"))
        app.paint()
        await pilot.pause()
        # Modal rendering is a different screen; inspect the restored reader.
        assert r.scroll_y == (r.max_scroll_y if bottom else 12)
        assert observed
        assert all(y == (end if bottom else 12) for y, end in observed)
        assert v.presentation.turns["active"].status == "running"
        assert [f["type"] for f in frames(c)] == ["steer"]


@pytest.mark.asyncio
async def test_async_dialog_options_free_text_and_draft_are_independent():
    c, v = setup()
    ask(v, options=["a", "b"])
    pending_async(v)[0].data["questions"].append(
        dict(title="Second question?", options=[])
    )
    v.draft = "Unrelated draft"
    v.attachments = [{"name": "keep", "image": False, "content": {}}]
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+t")
        dialog = app.screen
        assert isinstance(dialog, AsyncQuestionDialog)
        assert isinstance(dialog.focused, PickerList)
        await pilot.press("ctrl+j", "enter")
        assert dialog.answers[0] == "b" and not frames(c)
        await pilot.press("i", "o", "k", "escape")
        assert app.screen is dialog and not frames(c)
        await pilot.press("enter")
        await pilot.pause()
        sent = frames(c)
        assert len(sent) == 1 and sent[0]["type"] == "steer"
        assert sent[0]["prompt"] == (
            "补充回答：\n\n问题：Which directory?\n回答：b"
            "\n\n问题：Second question?\n回答：ok"
        )
        assert not sent[0].get("files") and not sent[0].get("images")
        assert app.query_one(Composer).text == "Unrelated draft"
        assert v.attachments and not pending_async(v)


@pytest.mark.asyncio
async def test_cancel_and_stale_question_never_interrupt_or_send():
    c, v = setup()
    ask(v)
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+t", "escape")
        assert not isinstance(app.screen, AsyncQuestionDialog)
        assert pending_async(v) and not frames(c)
        await pilot.press("ctrl+t")
        dialog = app.screen
        dialog.query_one("#answer", ModalEditor).load_text("answer")
        v.event(dict(type="turn_steered", msg_id="other", turn_id="active",
                     prompt="already answered elsewhere"))
        await pilot.press("enter")
        assert app.screen is dialog and not frames(c)
        result = dialog.query_one("#answer-result", Static)
        assert "no longer pending" in str(result.render())


@pytest.mark.asyncio
async def test_visiting_questions_does_not_confirm_unsubmitted_drafts():
    c, v = setup()
    ask(v)
    pending_async(v)[0].data["questions"].append(
        dict(title="Second question?", options=[])
    )
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+t")
        dialog = app.screen
        dialog.query_one("#answer", ModalEditor).load_text("unconfirmed")
        await pilot.press("ctrl+right")
        dialog.query_one("#answer", ModalEditor).load_text("confirmed")
        await pilot.press("enter")
        assert dialog.index == 0 and not frames(c)
        assert dialog.query_one("#answer", ModalEditor).text == "unconfirmed"
        await pilot.press("enter")
        assert len(frames(c)) == 1


@pytest.mark.asyncio
async def test_remapped_question_submit_and_vim_insert_do_not_send_early():
    c, v = setup()
    ask(v)
    c.keys = KeyConfig({"question": {"confirm": ["ctrl+y"]}})
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+t", "i", "a", "enter", "b", "escape")
        editor = app.screen.query_one("#answer", ModalEditor)
        assert editor.text == "a\nb" and not frames(c)
        await pilot.press("ctrl+y")
        assert frames(c)[0]["type"] == "steer"


@pytest.mark.asyncio
async def test_question_answer_is_session_scoped_and_can_retry_rejection():
    c, v = setup()
    ask(v)
    c.attached_sid = "other"
    assert await c.answer_async("s", ["question"], "answer")
    sent = frames(c)[0]
    assert sent["sid"] == "s" and sent["type"] == "steer"
    assert not await c.answer_async("s", ["question"], "duplicate")
    v.event(dict(type="error", msg_id=sent["msg_id"], message="rejected"))
    assert pending_async(v)
    assert await c.answer_async("s", ["question"], "retry")
    assert len(frames(c)) == 2


@pytest.mark.asyncio
async def test_idle_question_answer_starts_new_turn_without_interrupt():
    c, v = setup()
    ask(v)
    v.state = "idle"
    assert await c.answer_async("s", ["question"], "answer")
    assert frames(c)[0]["type"] == "query"


def test_question_keys_are_configurable_and_indexed():
    keys = KeyConfig({"question": {"confirm": ["ctrl+y"]}})
    assert list(keys.layers["question"]["confirm"]) == ["ctrl+y"]
    assert "Async question" in keys.help()


def test_canonical_reply_leaves_other_native_questions_pending():
    _, v = setup()
    ask(v)
    ask(v, "second")
    pending_async(v)[1].data["questions"][0]["title"] = "Other question?"
    v.event(dict(
        type="turn_steered", msg_id="reply", turn_id="active",
        prompt=supplemental_answer_prompt([("Which directory?", "src")]),
    ))
    assert [b.id for b in pending_async(v)] == ["second"]


def test_ambiguous_canonical_reply_preserves_all_candidates():
    _, v = setup()
    ask(v)
    ask(v, "second")
    v.event(dict(
        type="turn_steered", msg_id="reply", turn_id="active",
        prompt=supplemental_answer_prompt([("Which directory?", "src")]),
    ))
    assert [b.id for b in pending_async(v)] == ["question", "second"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", " ", "\n\t"])
async def test_blank_blocking_answer_keeps_question_pending(text):
    from tests.test_tui_workspace import emit

    c, _ = setup()
    emit(c, "ask_user", ask_id="ask", question="Where?",
         allow_text=True, options=[])
    assert not await c.answer(text)
    assert c._pending_ask_for_attached()["ask_id"] == "ask"
    assert not frames(c)


@pytest.mark.asyncio
async def test_dialog_keeps_one_native_message_per_web_answer_envelope():
    c, v = setup()
    ask(v)
    ask(v, "second")
    pending_async(v)[1].data["questions"][0]["title"] = "Other question?"
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        for title in ("Which directory?", "Other question?"):
            await pilot.press("ctrl+t")
            dialog = app.screen
            assert dialog.questions == [dict(title=title, options=[])]
            dialog.query_one("#answer", ModalEditor).load_text("src")
            await pilot.press("enter")
            await pilot.pause()
            sent = frames(c)[-1]
            assert sent["prompt"] == supplemental_answer_prompt([(title, "src")])
            v.event(dict(type="turn_steered", msg_id=sent["msg_id"],
                         turn_id="active", prompt=sent["prompt"]))
        assert len(frames(c)) == 2
        assert not pending_async(v)
