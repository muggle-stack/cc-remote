"""Assert the real Kitty protocol, not just the presence of an image widget."""

from PIL import Image
from rich.console import Console

from cc_remote.tui_graphics import OwnedTGP, StableTGPImage


def test_repaint_does_not_reupload_or_delete_other_images(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "textual_image.renderable.tgp._send_tgp_message",
        lambda **kw: sent.append(kw),
    )
    image = Image.new("RGB", (40, 20), "green")
    first, second = StableTGPImage(image), StableTGPImage(image)
    console = Console(width=80, height=25)
    for widget in (first, second):
        list(console.render(widget.render()))
    ids = [first._renderable.terminal_image_id,
           second._renderable.terminal_image_id]
    assert ids[0] != ids[1]
    uploads = sum("payload" in m for m in sent)
    sent.clear()
    for _ in range(30):
        list(console.render(first.render()))
        list(console.render(second.render()))
    assert uploads == 2 and not sent
    first.image = None
    assert sent == [dict(a="d", d="I", i=ids[0], q=2)]
    assert second._renderable.terminal_image_id == ids[1]
    second.on_unmount()
    assert sent[-1] == dict(a="d", d="I", i=ids[1], q=2)
    image.close()


def test_replacing_pixels_releases_only_previous_upload(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "textual_image.renderable.tgp._send_tgp_message",
        lambda **kw: sent.append(kw),
    )
    image = Image.new("RGB", (30, 20), "red")
    widget = StableTGPImage(image)
    console = Console(width=80)
    list(console.render(widget.render()))
    old = widget._renderable.terminal_image_id
    widget.image = image
    list(console.render(widget.render()))
    assert widget._renderable.terminal_image_id != old
    assert [m for m in sent if m.get("a") == "d"] == [
        dict(a="d", d="I", i=old, q=2)
    ]
    widget.image = None
    image.close()


def test_resize_updates_placement_without_reupload(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "textual_image.renderable.tgp._send_tgp_message",
        lambda **kw: sent.append(kw),
    )
    with Image.new("RGB", (1200, 800), "red") as image:
        renderable = OwnedTGP(image, "auto", "auto")
        list(Console(width=80, height=25).render(renderable))
        old = renderable.terminal_image_id
        sent.clear()
        list(Console(width=30, height=15).render(renderable))
        assert renderable.terminal_image_id == old
        assert len(sent) == 1 and sent[0]["a"] == "p"
        assert sent[0]["p"] == 1 and sent[0]["i"] == old
        renderable.cleanup()
