"""Stable, individually owned Kitty placements for textual-image 0.13.2."""

from math import ceil, floor

from textual_image.renderable import tgp
from textual_image.widget._base import Image as BaseImage


class OwnedTGP(tgp.Image):
    placement_size = None
    source_box = None
    viewport_size = None
    placement_signature = None
    segments_signature = None
    segments = ()

    def __rich_console__(self, console, options):
        size = self.viewport_size or self._render_size.get_cell_size(
            options.max_width, options.max_height, tgp.get_cell_size()
        )
        if max(size) > len(tgp._NUMBER_TO_DIACRITIC):
            raise ValueError("Image too large to render")
        if self.terminal_image_id is None:
            self.upload_original()
        box = self.source_box or (
            0, 0, self._image_data.width, self._image_data.height,
        )
        signature = (self.terminal_image_id, size, box)
        if signature != self.placement_signature:
            x, y, right, bottom = box
            # One named virtual placement, updated in place without deleting
            # the uploaded pixels. Kitty applies cropping/scaling itself.
            tgp._send_tgp_message(
                a="p", i=self.terminal_image_id, p=1, U=1, q=2, C=1,
                c=size[0], r=size[1], x=x, y=y, w=right-x, h=bottom-y,
            )
            self.placement_signature = signature
        self.placement_size = size
        # Scrolling moves these same Unicode cells; do not rebuild the dense
        # diacritic strings on every repaint of an unchanged image.
        signature = (self.terminal_image_id, size)
        if signature != self.segments_signature:
            self.segments = tuple(self._render_diacritics(*size))
            self.segments_signature = signature
        yield from self.segments

    def upload_original(self):
        # Avoid the dependency's redundant full-size resize/copies and its
        # quadratic slicing of the unconsumed PNG payload on every chunk.
        data = self._image_data.to_base64()
        self.terminal_image_id = next(tgp.Image._image_id_counter)
        for offset in range(0, len(data), 4096):
            tgp._send_tgp_message(
                i=self.terminal_image_id, f=100, q=2,
                m=int(offset + 4096 < len(data)),
                payload=data[offset:offset + 4096],
            )

    def cleanup(self):
        if self.terminal_image_id is not None:
            # The dependency sends a=d,I=<id>, which defaults to deleting all
            # visible placements. IDs require d=I,i=<id>, not image numbers.
            # https://sw.kovidgoyal.net/kitty/graphics-protocol/#deleting-images
            tgp._send_tgp_message(
                a="d", d="I", i=self.terminal_image_id, q=2
            )
            self.terminal_image_id = None


class StableTGPImage(BaseImage, Renderable=OwnedTGP):
    """Keep uploaded pixels until image replacement or cache eviction."""

    render_size = None
    viewport = None

    def set_view(self, source, box, columns, rows):
        if self.image is not source:
            self.image = source
        viewport = ((floor(box[0]), floor(box[1]),
                     ceil(box[2]), ceil(box[3])), (columns, rows))
        if self.viewport != viewport:
            self.viewport = viewport
            self.refresh()

    def render(self):
        if self.image is None:
            return ""
        size = self._get_styled_size()
        if self._renderable is None:
            self._renderable = self._Renderable(self.image, *size)
        elif self.render_size != size:
            self._renderable._render_size = tgp.ImageSize(
                self._image_width, self._image_height, *size,
            )
        self.render_size = size
        if self.viewport:
            self._renderable.source_box, self._renderable.viewport_size = (
                self.viewport
            )
        return self._renderable

    def on_unmount(self):
        self.image = None
