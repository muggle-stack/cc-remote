"""Full queued prompts are one-shot editor payloads, never Reports."""

from cc_remote.tui_panels import QueueEdit
from cc_remote.tui_state import WorkspaceState
from tests.test_tui_workspace import client, emit


def test_queue_detail_is_private_and_discarded_after_editor_closes():
    c = client()
    editor = QueueEdit(c, "s", "m")
    request = editor.request_id
    c.queue_reads[request] = ("s", "m")
    values = dict(request_id=request, msg_id="m", prompt="private-full-prompt")
    emit(c, "queued_query_detail", **values)
    assert c.queue_details[request]["prompt"] == values["prompt"]
    assert "queued_query_detail" not in c.workspace.view("s").presentation.reports
    assert "private-full-prompt" not in c.workspace.view("s").render()[0]
    editor.on_unmount()
    assert not c.queue_details and not c.queue_reads
    emit(c, "queued_query_detail", **values)
    assert not c.queue_details
    assert "queued_query_detail" not in c.workspace.view("s").presentation.reports


def test_unsolicited_queue_detail_cannot_enter_any_report_projection():
    state = WorkspaceState()
    for sid in ("s", None):
        state.event(dict(type="queued_query_detail", sid=sid, prompt="private"))
    assert not state.reports and not state.views
