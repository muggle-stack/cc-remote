"""Inline image layout without putting pixels in the Vim source document."""

import asyncio
from dataclasses import dataclass

from markdown_it import MarkdownIt
from rich.text import Text
from textual.containers import Container
from textual.widgets import Static

from cc_remote.tui import _safe_remote_text
from cc_remote.tui_markdown import MarkdownProjection
from cc_remote.tui_preview import (
    decode_image, local_reference, preview_request, references,
)


def close_decoded(task):
    """Release a decoder's eventual result after its view is cancelled."""
    if not task.cancelled() and task.exception() is None:
        task.result().close()


async def decode_async(result, **options):
    task = asyncio.create_task(
        asyncio.to_thread(decode_image, result, **options)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(close_decoded)
        raise


@dataclass
class ImageSlot:
    key: tuple
    path: str
    start: int
    end: int
    row: int
    height: int


class ImageProjection:
    """Map image rows to source offsets for copy and jump history."""

    def __init__(self):
        self.slots = []
        self.content = MarkdownProjection()

    def without_images(self, position):
        return position - sum(
            max(0, min(position, s.end) - s.start) for s in self.slots
        )

    def source(self, position):
        return self.content.source(self.without_images(position))

    def with_images(self, position):
        shift = 0
        for slot in self.slots:
            if slot.start - shift > position:
                break
            shift += slot.end - slot.start
        return position + shift

    def display(self, position):
        return self.with_images(self.content.display(position))

    def extract(self, text, start, end):
        start, end = sorted((start, end))
        parts = []
        for slot in self.slots:
            if slot.end <= start or slot.start >= end:
                continue
            parts.append(text[start:max(start, slot.start)])
            start = min(end, slot.end)
        return "".join(parts) + text[start:end]

    def locate(self, view, position, starts):
        return view.locate(self.source(position), [
            (self.source(start), block) for start, block in starts
        ])

    def resolve(self, view, anchor, starts, length, *, fallback=0):
        return self.display(view.resolve(anchor, [
            (self.source(start), block) for start, block in starts
        ], self.source(length), fallback=self.source(fallback)))

    def shift_headers(self, updates):
        for start, _, old, new in reversed(updates):
            self.content.replace_header(self.without_images(start), old, new)
        changes = {start: len(new) - len(old) for start, _, old, new in updates}
        for slot in self.slots:
            delta = sum(n for start, n in changes.items() if start < slot.start)
            slot.start += delta
            slot.end += delta


class InlinePicture(Container):
    DEFAULT_CSS = """
    InlinePicture { position: absolute; padding: 0; margin: 0; }
    InlinePicture > * { padding: 0; margin: 0; }
    """

    def __init__(self, factory, decoded, width, height):
        super().__init__()
        self.picture = factory(decoded)
        self.styles.width = width
        self.styles.height = height
        self.picture.styles.width = width
        self.picture.styles.height = height

    def compose(self):
        yield self.picture

    def on_unmount(self):
        self.picture.image = None


class TranscriptViewport(Container):
    """Clip images inside the reader viewport, never in bottom chrome."""

    DEFAULT_CSS = """
    TranscriptViewport { height: 1fr; overflow: hidden hidden; }
    TranscriptViewport > .image-notice {
        position: absolute; height: 1; color: $text-muted;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    """
    MAX_SLOTS = 64
    MAX_CACHE = 8

    def __init__(self, reader):
        super().__init__(reader, id="transcript-viewport")
        self.reader = reader
        self.identity = None
        self.cache = {}
        self.errors = {}
        self.pending = {}
        self.pictures = {}
        self.parked = {}
        self.projection = ImageProjection()
        self.rich_lines = []
        self.render_width = 0
        self.generation = 0
        self.read_limit = asyncio.Semaphore(2)

    def on_mount(self):
        # Also hide placements when a modal covers the base screen (its paint
        # loop is intentionally paused while a picker owns the keyboard).
        self.set_interval(0.1, self.sync)

    def reset(self, identity):
        if identity == self.identity:
            return
        self.identity = identity
        self.generation += 1
        for task in self.pending.values():
            task.cancel()
        self.pending.clear()
        self.clear_pictures()
        for image in self.cache.values():
            image.close()
        self.cache.clear()
        self.errors.clear()
        self.projection = ImageProjection()
        self.rich_lines = []

    def clear_pictures(self):
        for _, widget in (*self.pictures.values(), *self.parked.values()):
            widget.display = False
            widget.remove()
        self.pictures.clear()
        self.parked.clear()

    def dimensions(self, key):
        image = self.cache.get(key)
        if image is None:
            return max(1, self.reader.content_size.width), 1
        from textual_image._terminal import get_cell_size

        cell = get_cell_size()
        width = max(1, min(160, self.reader.content_size.width - 2))
        scale = min(width * cell.width / image.width,
                    12 * cell.height / image.height)
        return (max(1, int(image.width * scale / cell.width)),
                max(1, int(image.height * scale / cell.height)))

    def project(self, text, starts, identity):
        self.reset(identity)
        self.projection = ImageProjection()
        self.projection.content = MarkdownProjection(
            text, self.app.client.keys.label("diagram_browser"),
        )
        self.render_width = max(8, self.reader.wrap_width)
        markdown, markdown_starts = self.projection.content.project(
            starts, self.render_width
        )
        # Parse complete paragraphs, retaining Markdown's link handling (and
        # rejecting external URLs). Only actual rendered assistant text counts.
        edits = []
        for index, (start, block) in enumerate(starts):
            if not self.app.graphics:
                break
            if block.role != "assistant" or block.channel == "thinking":
                continue
            end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
            body_start = text.find("\n", start, end) + 1
            body = text[body_start:end][:65536]
            seen = set()
            lines = body.splitlines(keepends=True)
            for token in MarkdownIt("commonmark").parse(body):
                if token.type != "inline" or not token.map:
                    continue
                position = body_start + sum(
                    len(line) for line in lines[:token.map[1]]
                )
                refs = references(token.content)
                # Reference-style Markdown links need the full document's
                # resolved tokens, not a fresh parse of the inline source.
                for child in token.children or []:
                    if child.type in {"image", "link_open"}:
                        ref = local_reference(
                            child.attrGet("src") or child.attrGet("href") or ""
                        )
                        if ref:
                            refs.append(ref)
                for ref in refs:
                    if ref.kind != "image" or ref.path in seen:
                        continue
                    seen.add(ref.path)
                    if len(edits) >= self.MAX_SLOTS:
                        break
                    key = (block.id, ref.path)
                    edits.append((self.projection.content.paragraph_end(position),
                                  key, ref.path))
        parts, new_starts = Text(), []
        text = markdown.plain
        previous = shift = 0
        for position, key, path in sorted(edits, key=lambda e: e[0]):
            parts.append_text(markdown[previous:position])
            height = self.dimensions(key)[1]
            begin = position + shift
            parts.append("\n" * height)
            self.projection.slots.append(ImageSlot(
                key, path, begin, begin + height,
                text[:position].count("\n") + shift, height,
            ))
            previous = position
            shift += height
        parts.append_text(markdown[previous:])
        for start, block in markdown_starts:
            new_starts.append((self.projection.with_images(start), block))
        keys = {s.key for s in self.projection.slots}
        self.errors = {k: v for k, v in self.errors.items() if k in keys}
        for key in set(self.pending) - keys:
            self.pending.pop(key).cancel()
        self.rich_lines = parts.split("\n", allow_blank=True)
        return parts.plain, new_starts

    def sync(self):
        if not self.is_mounted:
            return
        reader = self.reader
        visible = {}
        if self.app.graphics and len(self.app.screen_stack) == 1:
            for slot in self.projection.slots:
                y = reader.wrapped_document.location_to_offset((slot.row, 0)).y
                y -= int(reader.scroll_y)
                if (y + slot.height > 0
                        and y < reader.scrollable_content_region.height):
                    visible[slot.key] = (slot, y)
        for key in set(self.pictures) - visible.keys():
            entry = self.pictures.pop(key)
            _, widget = entry
            widget.display = False
            self.parked[key] = entry
        keys = {s.key for s in self.projection.slots}
        for key in list(self.parked):
            if key not in keys or key not in self.cache:
                self.parked.pop(key)[1].remove()
        for key, (slot, y) in visible.items():
            width, height = self.dimensions(key)
            signature = (key in self.cache, width, height, self.errors.get(key))
            old = self.pictures.get(key) or self.parked.pop(key, None)
            if old and old[0] != signature:
                old[1].display = False
                old[1].remove()
                old = None
            if old is None:
                if key in self.cache:
                    widget = InlinePicture(
                        self.app.graphics, self.cache[key], width, height
                    )
                else:
                    widget = Static(
                        Text(self.errors.get(key, "Loading image…")),
                        classes="image-notice",
                    )
                    widget.styles.width = width
                self.pictures[key] = (signature, widget)
                self.mount(widget)
            else:
                self.pictures[key] = old
            widget = self.pictures[key][1]
            widget.display = True
            widget.styles.offset = (reader.gutter.left, y + reader.gutter.top)
            if (key not in self.cache and key not in self.errors
                    and key not in self.pending):
                self.pending[key] = asyncio.create_task(
                    self.load(slot, self.generation)
                )
        while len(self.parked) > self.MAX_CACHE:
            self.parked.pop(next(iter(self.parked)))[1].remove()
        self.trim_cache()

    def trim_cache(self):
        # A tall viewport can temporarily show more than MAX_CACHE images.
        # Bound residency again immediately when those images scroll away.
        while len(self.cache) > self.MAX_CACHE:
            victim = next(
                (k for k in self.cache if k not in self.pictures), None
            )
            if victim is None:
                break
            if victim in self.parked:
                self.parked.pop(victim)[1].remove()
            self.cache.pop(victim).close()

    async def load(self, slot, generation):
        decoded = None
        try:
            sid = self.identity[0]
            async with self.read_limit:
                result = await preview_request(self.app.client, sid, slot.path)
                if result.get("type") == "preview_authorization_required":
                    raise ValueError("Image requires authorization · "
                                     + self.app.client.keys.label("preview"))
                # Bounded decode off the input loop; cancellation still owns
                # and disposes the eventual result of the worker thread.
                decoded = await decode_async(result)
            if generation != self.generation or not self.is_mounted:
                return
            self.app.remember()
            self.cache[slot.key] = decoded
            decoded = None
            # Evict only offscreen images; current page is bounded by the
            # viewport. Their slots keep a cheap placeholder for lazy reload.
            self.trim_cache()
            self.app.rendered_version = -1
            self.app.paint()
        except (ValueError, OSError) as exc:
            if generation == self.generation:
                self.errors[slot.key] = _safe_remote_text(str(exc))
                self.sync()
        finally:
            if decoded is not None:
                decoded.close()
            if self.pending.get(slot.key) is asyncio.current_task():
                self.pending.pop(slot.key, None)

    def on_mouse_scroll_up(self, event):
        # Pixel children cover the blank source rows, so wheel events there
        # must reach the same reader as wheel events on ordinary message text.
        self.reader.on_mouse_scroll_up()
        self.reader.scroll_relative(y=-3, animate=False)
        event.stop()

    def on_mouse_scroll_down(self, event):
        self.reader.scroll_relative(y=3, animate=False)
        self.reader.on_mouse_scroll_down()
        event.stop()

    def on_unmount(self):
        self.reset(None)
