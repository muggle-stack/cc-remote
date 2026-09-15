"""Cell-aligned sequence diagrams; no browser or graphics dependency."""

import re

from rich.cells import cell_len
from rich.text import Text

from cc_remote.tui_diagram_text import DiagramLimit, MAX_OUTPUT, label

MESSAGE = re.compile(
    r"([^\s:]+?)\s*(<<-->>|<<->>|--?>>?|--?\)|--?[xX])"
    r"\s*([+-]?[^\s:]+)\s*:\s*(.*)",
    re.S,
)
PARTICIPANT = re.compile(
    r"(?:(create)\s+)?(participant|actor)\s+([^\s@]+)"
    r"(?:\s+as\s+(.+))?\Z",
    re.S,
)
NOTE = re.compile(
    r"note\s+(over|left of|right of)\s+([^:]+):\s*(.*)",
    re.I | re.S,
)
BLOCK = re.compile(r"(?:loop|alt|opt|par|critical|break|rect|box)\b")


def arrow_head(arrow, right):
    if arrow.lower().endswith("x"):
        return "×"
    if arrow.endswith(")"):
        return "▷" if right else "◁"
    if arrow.endswith(">>"):
        return "▶" if right else "◀"
    return "┤" if right else "├"


class Sequence:
    def __init__(self, projection):
        self.projection = projection
        self.canvas = projection.canvas
        self.people = {}
        self.events = []
        self.active = {}
        self.dead = set()
        self.unborn = set()
        self.pending_destroy = None
        self.depth = 0
        self.counter = None
        self.saved_counter = 1
        self.increment = 1

    def person(self, identity):
        if identity not in self.people:
            if len(self.people) >= 64:
                raise DiagramLimit("Too many sequence participants")
            self.people[identity] = identity

    def parse(self):
        for _, text in self.projection.rows():
            participant = PARTICIPANT.fullmatch(text)
            message = MESSAGE.fullmatch(text)
            note = NOTE.fullmatch(text)
            if participant:
                create, kind, identity, alias = participant.groups()
                self.person(identity)
                self.people[identity] = (
                    "actor · " if kind == "actor" else ""
                ) + (alias or identity)
                if create:
                    self.unborn.add(identity)
                    self.events.append(("create", identity))
            elif message:
                sender, arrow, receiver, value = message.groups()
                change = receiver[0] if receiver[0] in "+-" else ""
                receiver = receiver.lstrip("+-")
                self.person(sender)
                self.person(receiver)
                self.events.append(
                    ("message", sender, arrow, receiver, value, change)
                )
            elif note:
                position, identities, value = note.groups()
                people = [v.strip() for v in identities.split(",")]
                if len(people) > 2 or any(not p for p in people):
                    self.events.append(("source", text))
                    continue
                for identity in people:
                    self.person(identity)
                self.events.append(("note", position.lower(), people, value))
            elif re.fullmatch(r"(?:activate|deactivate|destroy)\s+\S+", text):
                action, identity = text.split()
                self.person(identity)
                self.events.append((action, identity))
            elif BLOCK.match(text):
                self.events.append(("open", text))
                if text.startswith(("rect ", "box ")):
                    self.projection.browser.append("sequence group styling")
            elif text == "end":
                self.events.append(("close",))
            elif re.match(r"(?:else|and|option)\b", text):
                self.events.append(("branch", text))
            elif re.fullmatch(
                r"autonumber(?:\s+(?:off|resume|\d{1,32}(?:\s+\d{1,32})?))?",
                text,
            ):
                self.events.append(("number", text.split()[1:]))
            else:
                self.events.append(("source", text))

    def emit(self, row):
        # Rows are already cell-aligned. Do not pass them through label(),
        # which would decode entities a second time or strip literal quotes.
        self.canvas.text.append(row.rstrip() + "\n")
        if len(self.canvas.text) > MAX_OUTPUT:
            raise DiagramLimit("Sequence exceeds terminal output limit")

    def base(self):
        row = [" "] * self.width
        for identity, x in self.positions.items():
            if identity not in self.dead | self.unborn:
                row[x] = "┃" if self.active.get(identity, 0) else "│"
        for level in range(min(self.depth, self.margin)):
            row[level] = "│"
            row[-1 - level] = "│"
        return row

    def caption(self, value, left, right, *, frame=False):
        if frame:
            self.rule("╭", "╮", left, right)
            left, right = left + 1, right - 1
        available = max(2, right - left + 1)
        for part in label(value).splitlines() or [""]:
            for line in Text(part).wrap(self.canvas.console, available):
                row = self.base()
                if frame:
                    row[left - 1], row[right + 1] = "│", "│"
                padding = max(0, (available - line.cell_len) // 2)
                self.emit(
                    "".join(row[:left])
                    + " " * padding
                    + line.plain
                    + " " * (available - padding - line.cell_len)
                    + "".join(row[left + available :])
                )
        if frame:
            self.rule("╰", "╯", left - 1, right + 1)

    def rule(self, start, end, left, right):
        row = self.base()
        row[left : right + 1] = ["─"] * (right - left + 1)
        row[left], row[right] = start, end
        self.emit("".join(row))

    def headers(self):
        rows = [
            Text(label(name)).wrap(self.canvas.console, self.lane - 1)
            for name in self.people.values()
        ]
        for index in range(max(map(len, rows), default=0)):
            cells = []
            for parts in rows:
                text = parts[index].plain if index < len(parts) else ""
                missing = self.lane - cell_len(text)
                cells.append(
                    " " * (missing // 2) + text + " " * (missing - missing // 2)
                )
            self.emit(" " * self.margin + "".join(cells))
        self.emit("".join(self.base()))

    def message(self, sender, arrow, receiver, value, change):
        a, b = self.positions[sender], self.positions[receiver]
        left, right = sorted((a, b))
        if self.counter is not None:
            value = f"{self.counter}. {value}"
            self.counter += self.increment
        stroke = "┄" if "--" in arrow else "─"
        if sender == receiver:
            side = 1 if self.width - a > 4 else -1
            end = a + side * min(5, self.lane // 2)
            left, right = sorted((a, end))
            self.caption(
                value,
                max(self.margin, left - 2),
                min(self.width - self.margin - 1, right + 2),
            )
            row = self.base()
            row[left : right + 1] = [stroke] * (right - left + 1)
            row[a] = "├" if side == 1 else "┤"
            row[end] = "╮" if side == 1 else "╭"
            self.emit("".join(row))
            row[end] = "│"
            for i in range(left + 1, right):
                row[i] = " "
            row[a] = "┃" if self.active.get(sender) else "│"
            self.emit("".join(row))
            row[left : right + 1] = [stroke] * (right - left + 1)
            row[end] = "╯" if side == 1 else "╰"
            row[a] = arrow_head(arrow, side < 0)
        else:
            self.caption(value, left + 1, right - 1)
            row = self.base()
            row[left : right + 1] = [stroke] * (right - left + 1)
            row[a] = (
                ("◀" if a < b else "▶")
                if arrow.startswith("<<")
                else ("├" if a < b else "┤")
            )
            row[b] = arrow_head(arrow, a < b)
        self.emit("".join(row))
        if change == "+":
            self.active[receiver] = self.active.get(receiver, 0) + 1
        elif change == "-":
            self.active[sender] = max(0, self.active.get(sender, 0) - 1)
        if self.pending_destroy:
            row = self.base()
            row[self.positions[self.pending_destroy]] = "×"
            self.emit("".join(row))
            self.dead.add(self.pending_destroy)
            self.pending_destroy = None
        self.emit("".join(self.base()))

    def render(self):
        self.parse()
        count = max(1, len(self.people))
        self.margin = 2 if self.canvas.width >= count * 5 + 4 else 0
        self.lane = (self.canvas.width - 2 * self.margin) // count
        if self.lane < 4:
            self.canvas.line("Narrow terminal: sequence records", style="dim")
            for identity, name in self.people.items():
                self.canvas.line(f"{identity}: {name}")
            for event in self.events:
                if event[0] == "source":
                    self.projection.source(event[1])
                else:
                    self.canvas.line(" · ".join(map(str, event)))
            return
        self.width = self.lane * count + 2 * self.margin
        self.positions = {
            name: self.margin + i * self.lane + self.lane // 2
            for i, name in enumerate(self.people)
        }
        self.headers()
        for event in self.events:
            action, *args = event
            if action == "message":
                self.message(*args)
            elif action == "note":
                position, people, value = args
                a, b = sorted(
                    self.positions[p] for p in (people[0], people[-1])
                )
                if position == "left of":
                    left, right = self.margin, max(self.margin + 3, a - 1)
                elif position == "right of":
                    left, right = min(a + 1, self.width - 4), self.width - 1
                else:
                    left = max(self.margin, a - self.lane // 2)
                    right = min(self.width - 1, b + self.lane // 2 - 1)
                self.caption("Note: " + value, left, right, frame=True)
            elif action in {"open", "branch", "close"}:
                # Saturate only drawing indentation, never logical nesting.
                level = min(
                    max(0, self.margin - 1),
                    max(0, self.depth - (action != "open")),
                )
                self.caption(
                    args[0] if args else "end",
                    level + 1,
                    self.width - level - 2,
                )
                corners = {
                    "open": ("┌", "┐"),
                    "branch": ("├", "┤"),
                    "close": ("└", "┘"),
                }
                self.rule(*corners[action], level, self.width - level - 1)
                if action == "open":
                    self.depth += 1
                elif action == "close":
                    self.depth = max(0, self.depth - 1)
            elif action == "number":
                parts = args[0]
                if parts == ["off"]:
                    self.counter = None
                elif parts == ["resume"]:
                    self.counter = self.saved_counter
                else:
                    self.counter = int(parts[0]) if parts else 1
                    self.increment = int(parts[1]) if len(parts) > 1 else 1
            elif action in {"activate", "deactivate"}:
                identity = args[0]
                self.active[identity] = max(
                    0,
                    self.active.get(identity, 0)
                    + (1 if action == "activate" else -1),
                )
                self.emit("".join(self.base()))
            elif action == "create":
                self.unborn.discard(args[0])
                self.caption(
                    "create " + args[0],
                    self.margin,
                    self.width - self.margin - 1,
                )
            elif action == "destroy":
                self.pending_destroy = args[0]
            else:
                self.projection.source(args[0])
            if self.counter is not None:
                self.saved_counter = self.counter


def render_sequence(projection):
    sequence = Sequence(projection)
    sequence.render()
