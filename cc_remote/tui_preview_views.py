"""Numbered file hints, read-only Vim Markdown, and pan/zoom image previews."""

from functools import partial

from rich.console import Console
from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from cc_remote.tui import _safe_remote_text
from cc_remote.tui_modal import Overlay, PickerList, hints
from cc_remote.tui_panels import PanelReader
from cc_remote.tui_preview import preview_request
from cc_remote.tui_inline_images import decode_async
from cc_remote.tui_image_viewer import ImageCanvas
from cc_remote.tui_keys import LAYERS
from cc_remote.tui_markdown import render_markdown


class FileHints(Overlay):
    """Nine hints per page: digits select immediately, without 1/10 ambiguity."""

    key_layer = "file_hints"
    local_actions = {"cancel", "down", "up", "parent", "open", "select_file"}

    def __init__(self, refs, open_preview=None):
        super().__init__()
        self.refs, self.page = refs, 0
        self.open_preview = open_preview

    def compose(self):
        with Vertical(classes="tui-panel"):
            yield Label("File previews · choose a numbered file")
            yield PickerList()
            yield Static("", id="hint-page", markup=False)
            yield hints()

    def on_mount(self):
        self.fill()
        self.query_one(OptionList).focus()

    def fill(self):
        listing = self.query_one(OptionList)
        listing.clear_options()
        for n, ref in enumerate(
            self.refs[self.page * 9 : self.page * 9 + 9], 1
        ):
            listing.add_option(
                Option(
                    Text(f"{self.app.client.keys.layer_label('file_hints', f'select_{n}')}"
                         f"  {ref.kind} · " + _safe_remote_text(ref.path)),
                    id=str(n),
                )
            )
        listing.highlighted = 0
        self.query_one("#hint-page", Static).update(
            f"Page {self.page + 1}/{(len(self.refs) + 8) // 9}"
        )

    def action_parent(self):
        self.page = max(0, self.page - 1)
        self.fill()

    def action_open(self):
        self.page = min((len(self.refs) - 1) // 9, self.page + 1)
        self.fill()

    def choose(self, number):
        index = self.page * 9 + number - 1
        if index < len(self.refs):
            self.query_one(OptionList).highlighted = number - 1
            if self.open_preview is not None:
                self.open_preview(self.refs[index])
            else:
                self.dismiss(self.refs[index])

    def action_select_file(self, number):
        self.choose(number)

    def on_option_list_option_selected(self, event):
        event.stop()
        self.choose(int(event.option.id))


class MarkdownReader(PanelReader):
    """Rendered text/styles, with the same read-only Vim grammar as chat."""

    source = ""
    rich_lines = None
    source_truncated = False
    local_truncated = False

    def show_markdown(self, source):
        self.source_truncated = len(source) > 256 * 1024
        self.source = _safe_remote_text(source[: 256 * 1024])
        self.reflow()

    def reflow(self):
        width = max(10, self.size.width - 2)
        console = Console(width=width)
        browser_key = (self.app.client.keys.layer_label("preview", "browser")
                       if self.is_mounted else "Space B")
        lines = console.render_lines(
            render_markdown(self.source, width, browser_key)[0],
            console.options, pad=False,
        )
        self.rich_lines = [
            Text.assemble(*((s.text, s.style) for s in line if not s.control))
            for line in lines[:5000]
        ]
        self.local_truncated = self.source_truncated or len(lines) > 5000
        if self.local_truncated:
            self.rich_lines.append(Text(
                "[Preview truncated locally — open the full file in Web]",
                style="yellow",
            ))
        selection, scroll = self.selection, self.scroll_offset
        self.load_text("\n".join(line.plain for line in self.rich_lines))
        self.selection = selection
        self.scroll_to(scroll.x, scroll.y, animate=False)
        if self.is_mounted and isinstance(self.screen, FilePreviewScreen):
            self.screen.update_preview_status()

    def get_line(self, index):
        if self.rich_lines is not None and index < len(self.rich_lines):
            return self.rich_lines[index].copy()
        return super().get_line(index)

    def on_resize(self):
        if self.source:
            self.reflow()


class FilePreviewScreen(Overlay):
    DEFAULT_CSS = """
    FilePreviewScreen #preview-image { height: 1fr; }
    FilePreviewScreen #preview-status { height: auto; }
    """
    key_layer = "preview"
    local_actions = {"cancel", "refresh", "browser"}

    async def action_browser(self):
        from cc_remote.tui_diagram_browser import open_session_browser
        try:
            self.client.notice = await open_session_browser(self.client, self.sid)
        except ValueError as error:
            self.client.notice = str(error)
        if self.is_mounted:
            self.query_one("#preview-status", Static).update(self.client.notice)

    def __init__(self, client, sid, ref, graphics=None):
        super().__init__()
        self.client, self.sid, self.ref, self.graphics = (
            client,
            sid,
            ref,
            graphics,
        )
        self.challenge = None
        self.worker = None
        self.revision = 0
        self.image_prefix = ()
        self.wrapper_truncated = False
        if ref.kind == "image":
            self.key_layer = "image"

    def compose(self):
        with Vertical(classes="tui-panel"):
            yield Label(_safe_remote_text(self.ref.path), markup=False)
            yield Static(
                "Reading from wrapper…", id="preview-status", markup=False
            )
            yield MarkdownReader()
            yield Vertical(id="preview-image")
            yield hints()

    def on_mount(self):
        reader = self.query_one(MarkdownReader)
        reader.display = self.ref.kind != "image"
        self.query_one("#preview-image").display = not reader.display
        if reader.display:
            reader.focus()
        self.action_refresh()

    async def on_key(self, event):
        if self.key_layer != "image":
            return
        # Textual bindings handle single keys. Multi-key actions are resolved
        # against the same configurable, indexed registry, not hidden Vim keys.
        event.stop()
        event.prevent_default()
        sequence = self.image_prefix + (event.key,)
        self.image_prefix = ()
        await self.consume_image_chord(sequence)

    async def consume_image_chord(self, sequence):
        keys = self.client.keys.layers["image"]
        for name, chords in keys.items():
            for chord in chords:
                parts = tuple(chord.split())
                if parts == sequence:
                    await super().action_dispatch_shortcut(name, chord)
                    return True
                if parts[:len(sequence)] == sequence:
                    self.image_prefix = sequence
        return bool(self.image_prefix)

    async def action_dispatch_shortcut(self, name, key):
        prefix = self.image_prefix
        self.image_prefix = ()
        if (self.key_layer == "image" and prefix
                and LAYERS["image"][name].action != "cancel"):
            if await self.consume_image_chord(prefix + tuple(key.split())):
                return
        await super().action_dispatch_shortcut(name, key)

    def action_pan(self, dx, dy):
        self.query_one(ImageCanvas).pan(dx, dy)

    def action_zoom(self, direction):
        self.query_one(ImageCanvas).zoom(direction)

    def action_fit(self):
        self.query_one(ImageCanvas).fit()

    def on_image_canvas_changed(self, event):
        if "pan" not in self.local_actions:
            return
        self.query_one("#preview-status", Static).update(
            f"Read-only · {event.zoom:.0%} of fit size"
        )

    def action_refresh(self):
        self.revision += 1
        if self.worker:
            self.worker.cancel()
        self.challenge = None
        self.local_actions = {"cancel", "refresh", "browser"}
        self.update_hint()
        self.worker = self.run_worker(partial(self.load, self.revision))

    async def load(self, revision, authorization=None):
        status = self.query_one("#preview-status", Static)
        if self.ref.kind == "image" and not self.graphics:
            status.update(
                "No supported terminal graphics protocol; use the Web UI"
            )
            return
        status.update("Reading from wrapper…")
        try:
            if authorization:
                result = await preview_request(
                    self.client, self.sid, challenge=authorization
                )
                if result.get("status") != "granted":
                    raise ValueError(
                        "File authorization was not granted; use Reload file"
                    )
            result = await preview_request(self.client, self.sid, self.ref.path)
            if revision != self.revision:
                return
            if result["type"] == "preview_authorization_required":
                self.challenge = {
                    "authorization_id": result["authorization_id"],
                    "request_id": result["request_id"],
                }
                self.local_actions = {"cancel", "refresh", "authorize", "browser"}
                self.update_hint()
                status.update(
                    "Outside session directory. "
                    + self.client.keys.layer_label(self.key_layer, "authorize")
                    + ": allow this exact file read\n"
                    + _safe_remote_text(result["resolved_path"])
                )
                return
            self.challenge = None
            self.local_actions = {"cancel", "refresh", "browser"}
            self.update_hint()
            self.wrapper_truncated = bool(result.get("truncated"))
            reader = self.query_one(MarkdownReader)
            image_area = self.query_one("#preview-image", Vertical)
            await image_area.remove_children()
            if result.get("format") == "image":
                if not self.graphics:
                    raise ValueError(
                        "No supported terminal graphics protocol; use the Web UI"
                    )
                decoded = await decode_async(result, thumbnail=False)
                if revision != self.revision or not self.is_mounted:
                    decoded.close()
                    return
                reader.display = False
                canvas = ImageCanvas(self.graphics, decoded)
                try:
                    await image_area.mount(canvas)
                finally:
                    if not canvas.is_mounted:
                        decoded.close()
                self.local_actions |= {"pan", "zoom", "fit"}
                self.update_hint()
                canvas.focus()
            else:
                if result.get("format") not in {"markdown", "text"}:
                    raise ValueError("This format requires the Web UI")
                reader.display = True
                reader.show_markdown(result.get("content", ""))
                reader.focus()
            self.update_preview_status()
        except (ValueError, OSError) as exc:
            if revision == self.revision and self.is_mounted:
                status.update(_safe_remote_text(str(exc)))

    def update_preview_status(self):
        reader = self.query_one(MarkdownReader)
        self.query_one("#preview-status", Static).update(
            "Read-only"
            + (" · truncated by wrapper" if self.wrapper_truncated else "")
            + (" · truncated locally — open full file in Web"
               if reader.display and reader.local_truncated else "")
        )

    def action_authorize(self):
        if self.challenge:
            authorization, self.challenge = self.challenge, None
            self.local_actions = {"cancel", "refresh", "browser"}
            self.update_hint()
            self.worker = self.run_worker(
                partial(self.load, self.revision, authorization)
            )

    def on_unmount(self):
        if self.worker:
            self.worker.cancel()
