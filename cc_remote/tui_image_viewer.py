"""Bounded source-pixel image navigation; never fetch again to pan or zoom."""

from dataclasses import dataclass

from PIL import Image
from textual.containers import Container
from textual.message import Message
from textual_image._terminal import get_cell_size


@dataclass
class ImageView:
    width: int
    height: int
    zoom: float = 1
    x: float = 0.5
    y: float = 0.5

    def bounds(self, pixels):
        scale = min(pixels[0] / self.width, pixels[1] / self.height)
        scale *= self.zoom
        width = min(self.width, pixels[0] / scale)
        height = min(self.height, pixels[1] / scale)
        left = max(0, min(self.width - width, self.x * self.width - width / 2))
        top = max(0, min(
            self.height - height, self.y * self.height - height / 2,
        ))
        self.x = (left + width / 2) / self.width
        self.y = (top + height / 2) / self.height
        return (left, top, left + width, top + height), scale

    def pan(self, dx, dy, pixels):
        box, _ = self.bounds(pixels)
        self.x += dx * (box[2] - box[0]) / self.width * 0.15
        self.y += dy * (box[3] - box[1]) / self.height * 0.15
        self.bounds(pixels)


class ImageCanvas(Container, can_focus=True, inherit_bindings=False):
    DEFAULT_CSS = """
    ImageCanvas { height: 1fr; width: 1fr; overflow: hidden hidden;
                  align: center middle; }
    """
    BINDINGS = []

    class Changed(Message):
        def __init__(self, zoom):
            super().__init__()
            self.zoom = zoom

    def __init__(self, factory, source):
        super().__init__()
        self.source = source
        self.view = ImageView(*source.size)
        self.picture = factory(None)
        self.frame = None
        self.signature = None
        self.pending = False

    def compose(self):
        yield self.picture

    def pixels(self):
        cell = get_cell_size()
        # Kitty's Unicode placeholder coordinates have a bounded alphabet.
        return (max(1, min(256, self.size.width)) * cell.width,
                max(1, min(256, self.size.height)) * cell.height)

    def redraw(self):
        if not self.pending:
            self.pending = True
            self.call_after_refresh(self.draw_frame)

    def on_mount(self):
        self.redraw()

    def on_resize(self):
        self.redraw()

    def draw_frame(self):
        self.pending = False
        if not self.is_mounted or not self.size.width or not self.size.height:
            return
        pixels = self.pixels()
        box, scale = self.view.bounds(pixels)
        signature = (box, scale, pixels)
        if signature == self.signature:
            return
        self.signature = signature
        cell = get_cell_size()
        cols = max(1, round((box[2] - box[0]) * scale / cell.width))
        rows = max(1, round((box[3] - box[1]) * scale / cell.height))
        cols = min(cols, self.size.width, 256)
        rows = min(rows, self.size.height, 256)
        self.picture.styles.width = cols
        self.picture.styles.height = rows
        if hasattr(self.picture, "set_view"):
            # Kitty retains the original pixels; pan/zoom only updates a small
            # placement command. No PIL resampling or PNG upload per keypress.
            self.picture.set_view(self.source, box, cols, rows)
            self.post_message(self.Changed(self.view.zoom))
            return
        # Resample directly from the original into a bounded viewport frame.
        # No giant intermediate scaled image and no repeated network reads.
        target = (cols * cell.width, rows * cell.height)
        ratio = min(1, 1600 / target[0], 1200 / target[1])
        target = tuple(max(1, round(n * ratio)) for n in target)
        frame = self.source.resize(target, Image.Resampling.BILINEAR, box)
        old = self.frame
        self.picture.image = frame
        self.frame = frame
        if old:
            old.close()
        self.post_message(self.Changed(self.view.zoom))

    def pan(self, dx, dy):
        self.view.pan(dx, dy, self.pixels())
        self.redraw()

    def zoom(self, direction):
        self.view.zoom = max(1, min(32, self.view.zoom * 1.5 ** direction))
        self.redraw()

    def fit(self):
        self.view = ImageView(*self.source.size)
        self.redraw()

    def on_unmount(self):
        self.picture.image = None
        if self.frame:
            self.frame.close()
        self.source.close()
