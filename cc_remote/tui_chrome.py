"""Quiet status-line hierarchy: neutral text, one accent, semantic warnings."""

from rich.cells import get_character_cell_size
from rich.color import Color
from rich.style import Style
from rich.text import Text

from cc_remote.tui import _safe_remote_text

ACCENT = "#94afc4"
WARNING = "#c4aa80"
MUTED = "dim"


def activity_sweep(text: Text, start: int, end: int, phase: float,
                   base: Style, highlight: Style) -> None:
    """Sweep foreground color in terminal cells, preserving text and backgrounds."""
    widths = [get_character_cell_size(char) for char in text.plain[start:end]]
    width = sum(widths)
    if not width or not base.color or not highlight.color:
        return
    text.stylize(Style(color=base.color, dim=False), start, end)
    radius = max(3, min(12, width * 0.18))
    center = -radius + (width + 2 * radius) * phase
    low, high = base.color.get_truecolor(), highlight.color.get_truecolor()
    cell = 0
    for index, cells in enumerate(widths, start):
        strength = max(0, 1 - abs(cell + cells / 2 - center) / radius)
        if strength:
            color = Color.from_rgb(*(
                round(a + (b - a) * strength) for a, b in zip(low, high)
            ))
            text.stylize(Style(color=color, dim=False), index, index + 1)
        cell += cells


def setting_style(kind, value):
    if kind == "model":
        return f"bold {ACCENT}"
    if kind in {"perm", "permission_profile"}:
        if (
            value in {"never", "bypassPermissions"}
            or "danger-full-access" in value
        ):
            return WARNING
        return "default"
    if kind == "effort":
        return "default"
    return MUTED


def settings_text(presentation):
    text = Text()
    for kind, key in (
        ("model", "model"),
        ("effort", "effort"),
        ("perm", "mode"),
        ("permission_profile", "profile"),
        ("web_search", "mode"),
        ("collaboration_mode", "mode"),
    ):
        value = presentation.settings.get(kind, {}).get(key)
        if value:
            if text:
                text.append(" · ", MUTED)
            text.append(
                _safe_remote_text(value), setting_style(kind, str(value))
            )
    if "fast" in presentation.settings:
        if text:
            text.append(" · ", MUTED)
        text.append(
            "Fast" if presentation.settings["fast"].get("on") else "Standard",
            ACCENT if presentation.settings["fast"].get("on") else MUTED,
        )
    return text


def hint_text(value):
    text = Text()
    for index, part in enumerate(value.split(" · ")):
        if index:
            text.append(" · ", MUTED)
        key, separator, label = part.partition(":")
        text.append(key, ACCENT if separator else MUTED)
        if separator:
            text.append(separator + label, MUTED)
    return text


def status_text(value, *, width=None):
    head, _, foot = value.partition("\n")
    foot = " ".join(foot.split())
    text = Text()
    for index, part in enumerate(head.split(" · ")):
        if index:
            text.append(" · ", MUTED)
        if part.startswith(("DRAFT", "READ")):
            style = f"bold {ACCENT}"
        elif part in {"read_only", "failed", "interrupted"}:
            style = WARNING
        elif (
            part.startswith(("running", "Processing", "queued "))
            and part != "queued 0"
        ):
            style = ACCENT
        else:
            style = MUTED
        text.append(_safe_remote_text(part), style)
    if foot:
        text.append("\n")
        text.append(hint_text(_safe_remote_text(foot)))
    if width is not None:
        lines = text.split("\n")
        for line in lines:
            line.truncate(max(1, width), overflow="ellipsis")
        text = Text("\n").join(lines)
    return text
