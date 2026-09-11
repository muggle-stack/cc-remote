"""Local open-session tabs; never delete, interrupt or take over a session."""

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from cc_remote.tui import _safe_remote_text
from cc_remote.tui_modal import ModalEditor, PickerList, hints
from cc_remote.tui_panels import ActionPicker


class SessionBuffers:
    def __init__(self):
        self.ids: list[str] = []

    def open(self, sid):
        if sid and sid not in self.ids:
            self.ids.append(sid)

    def rekey(self, old, new):
        self.ids = list(
            dict.fromkeys(new if sid == old else sid for sid in self.ids)
        )

    def neighbor(self, sid, direction):
        if not self.ids:
            return None
        if sid not in self.ids:
            return self.ids[0]
        return self.ids[(self.ids.index(sid) + direction) % len(self.ids)]

    def close(self, sid):
        if sid not in self.ids:
            return None
        index = self.ids.index(sid)
        self.ids.remove(sid)
        return self.ids[min(index, len(self.ids) - 1)] if self.ids else None


def buffer_title(client, sid):
    row = client.workspace.catalog.get(sid, {})
    return " ".join(
        _safe_remote_text(
            row.get("summary") or row.get("first_prompt") or sid
        ).split()
    )


def tab_line(client, width):
    width = max(0, width)
    if not width:
        return Text()
    ids = client.buffers.ids
    if not ids:
        line = Text(
            f"No open sessions · {client.keys.label('tree')}: tree · "
            f"{client.keys.label('new_session')}: new",
            style="dim",
        )
        line.truncate(width, overflow="ellipsis")
        return line
    active = ids.index(client.attached_sid) if client.attached_sid in ids else 0
    labels = {}

    def label_at(index):
        if index in labels:
            return labels[index]
        sid = ids[index]
        title = Text(buffer_title(client, sid))
        # Bound individual prompt-like titles, not the number of visible tabs.
        title.truncate(32, overflow="ellipsis")
        view = client.workspace.views.get(sid)
        badge = view.tab_badge() if view else None
        labels[index] = Text(
            f" {index + 1} {title.plain}",
            "bold white on #334466" if sid == client.attached_sid else "dim",
        )
        if badge:
            color = ("cyan" if badge == "running" else "green"
                     if badge == "completed" else "red")
            labels[index].append(" ●", f"bold not dim {color}")
        labels[index].append(" │")
        return labels[index]

    def markers(start, end):
        return 2 * (int(start > 0) + int(end < len(ids)))

    start, end = active, active + 1
    used = label_at(active).cell_len
    if used + markers(start, end) > width:
        # Very narrow terminals still show the active tab, not just arrows.
        label = label_at(active).copy()
        available = width - markers(start, end)
        if available < len(str(active + 1)) + 4:
            label.truncate(width, overflow="ellipsis")
            return label
        label.truncate(available, overflow="ellipsis")
        line = Text("‹ " if start else "")
        line.append_text(label)
        if end < len(ids):
            line.append(" ›")
        return line
    while True:
        choices = []
        if start:
            choices.append((start - 1, start - 1, end))
        if end < len(ids):
            choices.append((end, start, end + 1))
        # Grow a contiguous window around focus, trying the other side when
        # one neighbor is too wide. All costs include CJK cells and markers.
        choices.sort(key=lambda choice: abs(choice[0] - active))
        for index, left, right in choices:
            size = label_at(index).cell_len
            if used + size + markers(left, right) <= width:
                start, end = left, right
                used += size
                break
        else:
            break
    line = Text("‹ " if start else "")
    for index in range(start, end):
        line.append_text(label_at(index))
    if end < len(ids):
        line.append(" ›")
    return line


class BufferPicker(ActionPicker):
    """Search only opened tabs, including tabs in other engines/spaces."""

    def __init__(self, client):
        super().__init__(client, None, scope="Open sessions")
        self.initial_insert = True

    def compose(self) -> ComposeResult:
        with Vertical(classes="tui-panel"):
            yield Label("Open sessions · title / directory / ID", markup=False)
            yield ModalEditor(
                classes="search-editor",
                id="search",
                placeholder="Search open sessions",
            )
            yield PickerList()
            yield hints()

    def on_mount(self, event: events.Mount):
        # Install this specialized picker once, without also running the
        # inherited mount handler. Search remains ready for typing.
        event.prevent_default()
        self.install_keys()
        self.filter("")
        editor = self.query_one(ModalEditor)
        editor.focus()
        editor.set_mode("INSERT")

    def filter(self, value):
        listing = self.query_one(OptionList)
        selected = (
            listing.get_option_at_index(listing.highlighted).id
            if listing.highlighted is not None
            else None
        )
        listing.clear_options()
        terms = value.casefold().split()
        matches = []
        for sid in self.client.buffers.ids:
            row = self.client.workspace.catalog.get(sid, {})
            label = (
                f"{buffer_title(self.client, sid)} · "
                f"{row.get('engine', '')}/{row.get('space', 'code')} · "
                f"{row.get('cwd', '')} · {sid}"
            )
            if all(term in label.casefold() for term in terms):
                listing.add_option(
                    Option(Text(_safe_remote_text(label)), id=sid)
                )
                matches.append(sid)
        listing.highlighted = (
            matches.index(selected)
            if selected in matches
            else (0 if matches else None)
        )

    def open_form(self, sid):
        sid = self.client.workspace.rekeys.get(sid, sid)
        if sid in self.client.buffers.ids:
            self.dismiss(sid)
