"""Validated, local-only workspace key configuration (no executable actions)."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import string
import tomllib

from textual.binding import Binding
from textual.keys import Keys, _character_to_key


@dataclass(frozen=True)
class Shortcut:
    action: str
    label: str
    keys: tuple[str, ...]


GLOBAL = {
    "paste_image": Shortcut("paste_image", "Paste image", ("ctrl+v",)),
    "toggle_pane": Shortcut("toggle_pane", "Read/Draft", ()),
    "focus_draft": Shortcut("focus_draft", "Draft / next result", ("ctrl+j",)),
    "focus_read": Shortcut("focus_read", "Read / previous result", ("ctrl+k",)),
    "send": Shortcut("submit", "Send (global alias)", ()),
    "queue": Shortcut("queue", "Queue", ("ctrl+e",)),
    "stop": Shortcut("stop", "Stop current turn (keep queue)", ("ctrl+x",)),
    "sessions": Shortcut("sessions", "Session tree", ()),
    "answer": Shortcut("answer", "Answer question", ("ctrl+t",)),
    "complete": Shortcut("complete", "Completion", ("ctrl+space", "ctrl+@")),
    "quit": Shortcut("quit", "Quit", ("ctrl+q",)),
}
NORMAL = {
    "jump_back": Shortcut("jump_history(-1)", "Jump back", ("ctrl+o",)),
    "jump_forward": Shortcut(
        "jump_history(1)", "Jump forward (Tab)", ("ctrl+i", "tab")
    ),
    "preview": Shortcut("preview_files", "Preview files", ("space v",)),
    "diagram_browser": Shortcut(
        "diagram_browser", "Open diagram session in browser", ("space B",)
    ),
    "help": Shortcut("panel('Help')", "Help", ("space h",)),
    "goal": Shortcut("panel('Goal / Plan')", "Goal/Plan", ("space g",)),
    "usage": Shortcut("panel('Usage / Context')", "Usage", ("space u",)),
    "queue_details": Shortcut("panel('Queue')", "Queue details", ("space l",)),
    "actions": Shortcut("actions", "Actions", ("space a",)),
    "settings": Shortcut("panel('Settings')", "Settings", ("space s",)),
    "reports": Shortcut("panel('Reports')", "Reports/skills", ("space r",)),
    "background": Shortcut("panel('Background')", "Background", ("space b b",)),
    "buffer_previous": Shortcut(
        "cycle_buffer(-1)", "Previous session tab", ("H",)
    ),
    "buffer_next": Shortcut("cycle_buffer(1)", "Next session tab", ("L",)),
    "buffer_search": Shortcut(
        "search_buffers", "Search open sessions", ("space comma",)
    ),
    "buffer_close": Shortcut(
        "close_buffer", "Close session tab", ("space b d",)
    ),
    "status": Shortcut("panel('Status')", "Status", ("space t",)),
    "notices": Shortcut("panel('Notices')", "Notices", ("space n",)),
    "tree": Shortcut("sessions", "Session tree", ("space e",)),
    "engine": Shortcut("toggle_engine", "Claude/Codex", ("space c",)),
    "space": Shortcut("toggle_space", "Code/Work", ("space w",)),
    "quote": Shortcut("quote_selection", "Quote selection", ("space q",)),
    "new_session": Shortcut("new_session", "New session", ("space enter",)),
    "model": Shortcut("choose_setting('model')", "Model", ("space m",)),
    "permissions": Shortcut(
        "choose_setting('permission_profile')", "Permissions", ("space p",)
    ),
    "previous_user": Shortcut(
        "jump_message('user', -1)", "Previous user", ("left_square_bracket u",)
    ),
    "next_user": Shortcut(
        "jump_message('user', 1)", "Next user", ("right_square_bracket u",)
    ),
    "previous_assistant": Shortcut(
        "jump_message('assistant', -1)",
        "Previous answer",
        ("left_square_bracket a",),
    ),
    "next_assistant": Shortcut(
        "jump_message('assistant', 1)",
        "Next answer",
        ("right_square_bracket a",),
    ),
    "previous_message": Shortcut(
        "jump_message('', -1)", "Previous message", ("left_square_bracket m",)
    ),
    "next_message": Shortcut(
        "jump_message('', 1)", "Next message", ("right_square_bracket m",)
    ),
}


def key_label(value: str) -> str:
    labels = {"space": "Space", "enter": "Enter", "escape": "Esc",
              "comma": ",", "colon": ":", "slash": "/"}
    return " ".join(labels.get(k, k.replace("ctrl+", "Ctrl+").replace("alt+", "Alt+"))
                    for k in value.split())


LAYERS = {
    "reader": {
        "latest_user": Shortcut("latest_message('user')", "Latest user", ("g u",)),
        "latest_assistant": Shortcut("latest_message('assistant')", "Latest answer", ("g a",)),
        "first": Shortcut("read_start", "Start of loaded history", ("g g",)),
        "follow": Shortcut("read_follow", "Latest output / follow", ("G",)),
        "details": Shortcut("read_details", "Expand / collapse details", ("enter",)),
        "older": Shortcut("read_older", "Older history / detail page", ("o",)),
        "newer": Shortcut("read_newer", "Newer detail page", ("O",)),
        "close": Shortcut("read_close", "Collapse details / clear selection", ("escape",)),
        "command": Shortcut("command_editor", "Command editor", ("colon",)),
    },
    "draft": {
        "send": Shortcut("submit", "Send (Normal)", ("enter",)),
        "cancel": Shortcut("cancel_draft_command", "Cancel command editor", ("escape",)),
    },
    "tree_search": {
        "down": Shortcut("down", "Next result", ("down",)),
        "up": Shortcut("up", "Previous result", ("up",)),
        "choose": Shortcut("choose", "Open result", ("enter",)),
        "close": Shortcut("close", "Clear search / return to tree", ("escape",)),
    },
    "tree": {
        "down": Shortcut("down", "Move down (count)", ("j", "down")),
        "up": Shortcut("up", "Move up (count)", ("k", "up")),
        "first": Shortcut("first", "First / numbered row", ("g g",)),
        "last": Shortcut("last", "Last / numbered row", ("G",)),
        "fold": Shortcut("fold", "Fold / parent", ("h", "left")),
        "expand": Shortcut("expand", "Expand folder", ("l", "right")),
        "fold_all": Shortcut("fold_all", "Collapse all folders", ("H",)),
        "expand_all": Shortcut("expand_all", "Expand all folders", ("L",)),
        "rename": Shortcut("rename", "Rename session", ("r",)),
        "delete": Shortcut("delete", "Delete session (confirm)", ("d",)),
        "delete_direct": Shortcut(
            "delete_direct", "Delete session without confirmation", ("D",)
        ),
        "search": Shortcut("search", "Search", ("slash",)),
        "close": Shortcut("close", "Close tree", ("escape",)),
        "choose": Shortcut("choose", "Open session / toggle folder", ("enter",)),
    },
    "file_hints": {
        "close": Shortcut("cancel", "Back", ("escape",)),
        "down": Shortcut("down", "Next file", ("j", "down")),
        "up": Shortcut("up", "Previous file", ("k", "up")),
        "parent": Shortcut("parent", "Previous page", ("h",)),
        "open": Shortcut("open", "Next page", ("l",)),
        "choose": Shortcut("choose", "Open selected file", ("enter",)),
        **{f"select_{n}": Shortcut(f"select_file({n})", f"Open file {n}", (str(n),))
           for n in range(1, 10)},
    },
    "preview": {
        "close": Shortcut("cancel", "Back", ("escape",)),
        "browser": Shortcut("browser", "Open session in browser", ("alt+b",)),
        "refresh": Shortcut("refresh", "Reload file", ("r",)),
        "authorize": Shortcut("authorize", "Allow exact file read", ("a",)),
    },
    "panel": {
        "close": Shortcut("cancel", "Back", ("escape",)),
        "refresh": Shortcut("refresh", "Refresh", ("r",)),
        "actions": Shortcut("actions", "Actions", ("a",)),
        "edit": Shortcut("edit", "Edit", ("i",)),
        "hide": Shortcut("hide", "Hide goal", ("x",)),
        "search": Shortcut("search", "Search shortcut index (Help)", ("slash",)),
    },
    "picker": {
        "close": Shortcut("cancel", "Back", ("escape",)),
        "search": Shortcut("search", "Search (Insert)", ("slash",)),
        "edit": Shortcut("edit", "Edit / search", ("i",)),
        "down": Shortcut("down", "Next", ("j", "down")),
        "up": Shortcut("up", "Previous", ("k", "up")),
        "choose": Shortcut("choose", "Choose highlighted item", ("enter",)),
        "parent": Shortcut("parent", "Parent directory", ("h", "ctrl+left")),
        "open": Shortcut("open", "Browse directory", ("l", "ctrl+right")),
        "refresh": Shortcut("refresh", "Refresh", ("r", "ctrl+r")),
    },
    "form": {
        "close": Shortcut("cancel", "Normal / Back", ("escape",)),
        "help": Shortcut("field_help", "Field help", ("question_mark",)),
        "confirm": Shortcut("submit", "Apply / confirm action", ("enter",)),
        "advanced": Shortcut("advanced", "Edit structured field as JSON", ("ctrl+r",)),
    },
    "confirmation": {
        "yes": Shortcut("yes", "Yes / confirm", ("y",)),
        "close": Shortcut("cancel", "No / cancel", ("n", "escape")),
        "choose": Shortcut("choose", "Confirm highlighted choice", ("enter",)),
        "down": Shortcut("down", "Next choice", ("j", "down")),
        "up": Shortcut("up", "Previous choice", ("k", "up")),
    },
}

LAYERS["image"] = {
    **LAYERS["preview"],
    "left": Shortcut("pan(-1, 0)", "Pan left", ("h", "left")),
    "right": Shortcut("pan(1, 0)", "Pan right", ("l", "right")),
    "down": Shortcut("pan(0, 1)", "Pan down", ("j", "down")),
    "up": Shortcut("pan(0, -1)", "Pan up", ("k", "up")),
    "zoom_in": Shortcut("zoom(1)", "Zoom in", ("z i",)),
    "zoom_out": Shortcut("zoom(-1)", "Zoom out", ("z o",)),
    "fit": Shortcut("fit", "Fit image", ("z f",)),
}

LAYERS["question"] = {
    "close": Shortcut("cancel", "Normal / close (task continues)", ("escape",)),
    "confirm": Shortcut("submit", "Submit answer / next question", ("enter",)),
    "edit": Shortcut("edit", "Write an answer", ("i",)),
    "down": Shortcut("down", "Next option", ("j", "down")),
    "up": Shortcut("up", "Previous option", ("k", "up")),
    "next": Shortcut("question(1)", "Next question", ("ctrl+right",)),
    "previous": Shortcut("question(-1)", "Previous question", ("ctrl+left",)),
}

LAYERS["queue"] = {
    "close": Shortcut("cancel", "Back", ("escape",)),
    "down": Shortcut("down", "Next message", ("j", "down")),
    "up": Shortcut("up", "Previous message", ("k", "up")),
    "choose": Shortcut("choose", "Read full prompt", ("enter",)),
    "edit": Shortcut("edit", "Edit prompt", ("i",)),
    "delete": Shortcut("delete", "Cancel message (confirm)", ("d",)),
    "move_up": Shortcut("move(-1)", "Move earlier", ("K",)),
    "move_down": Shortcut("move(1)", "Move later", ("J",)),
}

MODAL_LAYERS = {
    "panel", "picker", "form", "file_hints", "preview", "confirmation",
    "image", "question", "queue",
}
for _layer in ("picker", "file_hints", "queue"):
    LAYERS[_layer].update({
        "first": Shortcut("first", "First result", ("home",)),
        "last": Shortcut("last", "Last result", ("end",)),
        "page_up": Shortcut("page_up", "Previous results page", ("pageup",)),
        "page_down": Shortcut("page_down", "Next results page", ("pagedown",)),
    })

for _layer in MODAL_LAYERS:
    LAYERS[_layer].update({
        "next_field": Shortcut("next_field", "Next field", ("tab",)),
        "previous_field": Shortcut("previous_field", "Previous field", ("shift+tab",)),
    })

SCOPES = {
    "keys": "Global", "normal": "Main Normal / Visual",
    "reader": "Reader Normal / Visual", "draft": "Draft Normal",
    "tree": "Session tree", "tree_search": "Session tree search",
    "panel": "Detail panel", "picker": "Picker / search",
    "form": "Form Normal", "file_hints": "File hints", "preview": "File preview",
    "confirmation": "Deletion confirmation", "image": "Image preview",
    "question": "Async question",
    "queue": "Server queue",
}


class KeyConfig:
    def __init__(self, data: dict | None = None):
        data = {} if data is None else data
        if not isinstance(data, dict) or data.keys() - {
            "keys",
            "normal",
            "vim",
            *LAYERS,
        }:
            raise ValueError("Unknown TUI key layer")
        vim = data.get("vim", {})
        if not isinstance(vim, dict) or vim.keys() - {"yank_highlight_ms"}:
            raise ValueError("Unknown TUI Vim option")
        self.yank_highlight_ms = vim.get("yank_highlight_ms", 200)
        if (type(self.yank_highlight_ms) is not int
                or not 0 <= self.yank_highlight_ms <= 5000):
            raise ValueError("vim.yank_highlight_ms must be 0..5000")
        self.global_keys = self._section(data.get("keys", {}), GLOBAL, False)
        self.normal_keys = self._section(data.get("normal", {}), NORMAL, True)
        self.layers = {
            name: self._section(data.get(name, {}), specs, True)
            for name, specs in LAYERS.items()
        }
        for name, layer in self.layers.items():
            flat = [key for values in layer.values() for key in values]
            if name not in {"tree", "reader", "image"} and any(
                len(key.split()) != 1 for key in flat
            ):
                raise ValueError("Panel shortcuts must be single keys")
            if len(flat) != len(set(flat)):
                raise ValueError("Conflicting panel shortcuts")
            if name in {"tree", "reader", "image"}:
                chords = [tuple(key.split()) for key in flat]
                if name == "tree" and any(chord[0].isdigit() for chord in chords):
                    raise ValueError("Tree digits are reserved for Vim counts")
                if any(
                    a != b and b[:len(a)] == a
                    for a in chords for b in chords
                ):
                    raise ValueError("Conflicting tree shortcut prefixes")
            global_flat = {
                key for values in self.global_keys.values() for key in values
            }
            if any(key.split()[0] in global_flat for key in flat):
                raise ValueError(
                    "Panel shortcuts conflict with global shortcuts"
                )
        seen: dict[tuple[str, ...], str] = {}
        for section in (self.global_keys, self.normal_keys):
            for name, shortcuts in section.items():
                for shortcut in shortcuts:
                    chord = tuple(shortcut.split())
                    for previous, owner in seen.items():
                        if chord[: len(previous)] == previous or (
                            previous[: len(chord)] == chord
                        ):
                            raise ValueError(
                                f"Conflicting TUI shortcuts: {owner} / {name}"
                            )
                    seen[chord] = name
        self.chords = {
            tuple(key.split()): NORMAL[name].action
            for name, shortcuts in self.normal_keys.items()
            for key in shortcuts
        }
        for layer in ("reader", "draft", "tree"):
            combined = [tuple(k.split()) for keys in self.layers[layer].values()
                        for k in keys]
            for chord in combined:
                for normal in self.chords:
                    # H/L intentionally mean folder folding in the tree and
                    # buffer switching in chat. Prefix shadowing is unsafe.
                    if layer == "tree" and chord == normal:
                        continue
                    if chord[:len(normal)] == normal or normal[:len(chord)] == chord:
                        raise ValueError(f"Conflicting {layer}/normal shortcut prefixes")

    @staticmethod
    def _section(data: dict, specs: dict, normal: bool) -> dict:
        if not isinstance(data, dict) or data.keys() - specs.keys():
            raise ValueError("Unknown TUI shortcut action")
        result = {name: spec.keys for name, spec in specs.items()}
        known = {key.value for key in Keys} | {
            "space",
            "tab",
            "escape",
            "ctrl+space",
        }
        known |= {_character_to_key(char) for char in string.punctuation}
        for name, values in data.items():
            if not isinstance(values, list) or not all(
                isinstance(value, str) for value in values
            ):
                raise ValueError(f"TUI shortcut {name} must be a string array")
            for value in values:
                tokens = value.split()
                if not 1 <= len(tokens) <= (3 if normal else 1):
                    raise ValueError(f"Invalid TUI chord for {name}")
                for token in tokens:
                    if token not in known and not re.fullmatch(
                        r"(?:(?:ctrl|alt)\+)?[a-zA-Z0-9@]", token
                    ):
                        raise ValueError(f"Invalid TUI key: {token}")
                if not normal and not (
                    tokens[0] in known or tokens[0].startswith("ctrl+")
                ):
                    raise ValueError("Printable shortcuts belong in [normal]")
            result[name] = tuple(" ".join(value.split()) for value in values)
        return result

    def bindings(self) -> list[Binding]:
        return [
            Binding(
                key,
                GLOBAL[name].action,
                GLOBAL[name].label,
                priority=True,
                show=name
                in {"toggle_pane", "sessions", "send", "queue", "quit"},
            )
            for name, keys in self.global_keys.items()
            for key in keys
        ]

    def label(self, name: str) -> str:
        keys = {**self.global_keys, **self.normal_keys}[name]
        if name == "send" and not keys:
            return self.layer_label("draft", "send") + " (Normal)"
        if name == "sessions" and not keys:
            return self.label("tree")
        if name == "tree" and not keys:
            keys = self.global_keys["sessions"]
        return " / ".join(map(key_label, keys)) if keys else "disabled"

    def layer_label(self, layer: str, name: str) -> str:
        keys = self.layers[layer][name]
        return " / ".join(map(key_label, keys)) if keys else "disabled"

    def match(self, layer: str, key: str) -> str | None:
        return next((name for name, keys in self.layers[layer].items()
                     if key in keys), None)

    def index(self, query: str = "", *, include_disabled=True) -> list[dict]:
        """Stable configuration IDs and effective bindings; safe for JSON/UI."""
        result = []
        sections = {"keys": GLOBAL, "normal": NORMAL, **LAYERS}
        values = {"keys": self.global_keys, "normal": self.normal_keys, **self.layers}
        for layer, specs in sections.items():
            for name, spec in specs.items():
                keys = values[layer][name]
                item = dict(id=f"{layer}.{name}", layer=layer, scope=SCOPES[layer],
                            action=spec.action, description=spec.label,
                            keys=list(keys), enabled=bool(keys),
                            display=" / ".join(map(key_label, keys)) or "disabled")
                haystack = " ".join(str(v) for v in item.values()).casefold()
                if (include_disabled or keys) and all(
                    word in haystack for word in query.casefold().split()
                ):
                    result.append(item)
        return result

    def lookup(self, key: str, *, layer: str | None = None) -> list[dict]:
        """Reverse lookup retains scope: Enter can mean different local actions."""
        key = " ".join(key.split())
        return [row for row in self.index(include_disabled=False)
                if key in row["keys"] and (layer is None or row["layer"] == layer)]

    def help(self) -> str:
        groups = {}
        for item in self.index(include_disabled=False):
            groups.setdefault(item["scope"], []).append(
                f"{item['display']:24} {item['description']}  [{item['id']}]"
            )
        groups["Vim feedback"] = [
            f"Yank highlight: {self.yank_highlight_ms} ms "
            "[vim.yank_highlight_ms] (0 disables)"
        ]
        return "\n\n".join(title + "\n" + "\n".join(rows)
                            for title, rows in groups.items())

    def layer_bindings(self, layer: str) -> list[Binding]:
        return [
            Binding(
                key,
                f"dispatch_shortcut({name!r}, {key!r})",
                LAYERS[layer][name].label,
                priority=True,
                show=False,
            )
            for name, keys in self.layers[layer].items()
            for key in keys
            if layer != "image" or len(key.split()) == 1
        ]

    def layer_help(self, layer: str, actions: set | None = None) -> str:
        return " · ".join(
            f"{'/'.join(map(key_label, keys))}: {LAYERS[layer][name].label}"
            for name, keys in self.layers[layer].items()
            if keys
            and (actions is None
                 or LAYERS[layer][name].action.split("(", 1)[0] in actions)
        )


def load_keys(path: str | None = None) -> KeyConfig:
    explicit = path or os.environ.get("CC_REMOTE_TUI_CONFIG")
    target = (
        Path(explicit).expanduser()
        if explicit
        else (
            Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            / "cc-remote"
            / "tui.toml"
        )
    )
    try:
        with target.open("rb") as stream:
            return KeyConfig(tomllib.load(stream))
    except FileNotFoundError as exc:
        if not explicit:
            return KeyConfig()
        raise ValueError(f"TUI config not found: {target}") from exc
    except (OSError, ValueError) as exc:
        raise ValueError(f"Invalid TUI config {target}: {exc}") from exc
