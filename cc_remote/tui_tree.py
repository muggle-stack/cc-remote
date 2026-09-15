"""A collapsible, catalog-backed session explorer (never scans the disk)."""

from rich.text import Text
from pathlib import PurePosixPath
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Static, Tree

from cc_remote.tui import _safe_remote_text
from cc_remote.protocol import CloseBtw, DeleteSession, RenameSession
from cc_remote.tui_modal import Overlay, PickerList, hints
from cc_remote.tui_settings import TextValue
from textual.widgets import Label, OptionList, TextArea


def tree_nodes(root):
    for child in root.children:
        yield child
        yield from tree_nodes(child)


def expanded_folders(tree):
    return {node.data for node in tree_nodes(tree.root)
            if node.data and node.data[0] != "session" and node.is_expanded}


class RenameDialog(TextValue):
    async def action_submit(self):
        title = self.query_one(TextArea).text.strip()
        if not 1 <= len(title) <= 200:
            self.query_one(Label).update("Name must contain 1–200 characters")
            return
        await super().action_submit()


class DeleteDialog(Overlay):
    """A captured session target; cancellation is the default choice."""

    key_layer = "confirmation"
    local_actions = {"cancel", "yes", "down", "up"}
    DEFAULT_CSS = """
    DeleteDialog .tui-panel { height: auto; max-height: 85%; }
    DeleteDialog OptionList { height: 4; }
    """

    def __init__(self, sid, title, cwd, *, archive=False, side_chat=False):
        super().__init__()
        self.sid, self.title_text, self.cwd = sid, title, cwd
        self.archive = archive
        self.side_chat = side_chat

    def compose(self):
        with Vertical(classes="tui-panel"):
            yield Label("Close side chat?" if self.side_chat else "Delete session?",
                        markup=False)
            yield Label(_safe_remote_text(self.title_text), markup=False)
            yield Label(_safe_remote_text(self.sid), markup=False)
            yield Label(_safe_remote_text(self.cwd), markup=False)
            yield Label(
                "Discard this side chat; the parent session stays open."
                if self.side_chat else
                "Permanent deletion; not just closing a local tab.\n"
                "Server deletion rules still apply.", markup=False,
            )
            if self.archive:
                yield Label(
                    "Codex must first archive its conversation tree.\n"
                    "Delete is sent only after archive confirmation.",
                    markup=False,
                )
            yield PickerList("No — keep session", "Yes — close side chat"
                             if self.side_chat else "Yes — delete session")
            yield hints()

    def on_mount(self):
        self.query_one(OptionList).highlighted = 0
        self.query_one(OptionList).focus()

    def action_yes(self):
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)

    def on_option_list_option_selected(self, event):
        event.stop()
        self.dismiss(event.option_index == 1)


class SessionTree(Tree, inherit_bindings=False):
    BINDINGS = []
    count = ""
    chord: tuple[str, ...] = ()

    def on_blur(self):
        self.count = ""
        self.chord = ()

    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        if self.app.shortcut_prefix:
            self.on_blur()
            await self.app.normal_shortcut(key)
            event.stop()
            event.prevent_default()
            return
        bindings = {
            tuple(keys.split()): name
            for name, values in self.app.client.keys.layers["tree"].items()
            for keys in values
        }
        if len(key) == 1 and key.isascii() and key.isdigit():
            self.count = str(min(100000, int((self.count or "0") + key)))
            event.stop()
            event.prevent_default()
            return
        chord = (*self.chord, key)
        pending = bool(self.chord)
        self.chord = ()
        action = bindings.get(chord)
        if not action and any(k[:len(chord)] == chord for k in bindings):
            self.chord = chord
        elif action:
            count, self.count = self.count, ""
            amount = max(1, int(count or "1"))
            if action in {"down", "up", "first", "last"}:
                self.get_node_at_line(0)
                line = (
                    self.cursor_line + amount * (1 if action == "down" else -1)
                    if action in {"down", "up"}
                    else int(count) - 1 if count
                    else 0 if action == "first" else self.last_line
                )
                self.move_cursor(self.get_node_at_line(
                    max(0, min(self.last_line, line))
                ))
            else:
                await self.local_action(action)
        else:
            self.count = ""
            if not pending and not await self.app.normal_shortcut(key):
                await super()._on_key(event)
                return
        event.stop()
        event.prevent_default()

    async def local_action(self, action):
        if action == "fold":
            node = self.cursor_node
            if node and node.is_expanded:
                node.collapse()
            elif node and node.parent and node.parent is not self.root:
                self.move_cursor(node.parent)
        elif action == "expand":
            node = self.cursor_node
            if node:
                node.expand()
        elif action in {"fold_all", "expand_all"}:
            node = self.cursor_node
            if action == "fold_all" and node:
                while node.parent and node.parent is not self.root:
                    node = node.parent
                self.move_cursor(node)
            selected = self.cursor_node
            for folder in self.root.children:
                (folder.collapse_all if action == "fold_all"
                 else folder.expand_all)()
            self.get_node_at_line(0)
            self.move_cursor(selected)
        elif action == "rename":
            self.parent.rename_selected()
        elif action in {"delete", "delete_direct"}:
            await self.parent.delete_selected(confirm=action == "delete")
        elif action == "search":
            self.parent.start_search()
        elif action == "close":
            self.app.action_sessions()
        elif action == "choose":
            self.action_select_cursor()


class TreeSearch(Input, inherit_bindings=False):
    BINDINGS = [b for b in Input.BINDINGS if b.action != "submit"]

    async def _on_key(self, event: events.Key) -> None:
        action = self.app.client.keys.match("tree_search", event.key)
        if action == "close":
            self.value = ""
            self.display = False
            self.parent.query_one(SessionTree).focus()
        elif action in {"up", "down"}:
            self.parent.move_selection(action == "down")
        elif action == "choose":
            await self.parent.open_search_result()
        else:
            await super()._on_key(event)
            return
        event.stop()
        event.prevent_default()


class SessionExplorer(Vertical):
    DEFAULT_CSS = """
    SessionExplorer {
        width: 34%; max-width: 48; min-width: 20; height: 1fr;
        border-right: solid $primary;
    }
    SessionExplorer Static { height: auto; }
    SessionExplorer Input { height: 3; }
    SessionExplorer Tree { height: 1fr; }
    """

    def __init__(self):
        super().__init__(id="session-explorer")
        self.signature = None
        self.scope = None
        self.expanded: dict[tuple[str, str], set[tuple[str, str]]] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="tree-heading", markup=False)
        yield TreeSearch(placeholder="Search title / folder / ID")
        yield SessionTree("Sessions")
        yield Static(
            f"{self.app.client.keys.label('help')}: help",
            markup=False,
        )

    def on_mount(self) -> None:
        self.query_one(TreeSearch).display = False
        self.query_one(SessionTree).show_root = False
        self.query_one(SessionTree).auto_expand = False
        self.display = False

    def start_search(self) -> None:
        search = self.query_one(TreeSearch)
        search.display = True
        search.focus()

    def rename_selected(self):
        node = self.query_one(SessionTree).cursor_node
        client = self.app.client
        if not node or not node.data or node.data[0] != "session":
            client.notice = "Select a session to rename, not a folder"
            return
        sid = node.data[1]
        row = client.visible_catalog().get(sid)
        if row is None:
            return
        engine, space = client.scope
        title = row.get("summary") or row.get("first_prompt") or ""

        async def renamed(result):
            if result is None:
                return
            if sid not in client.workspace.catalog:
                client.notice = "Session no longer exists; rename cancelled"
                return
            await client.execute_action(RenameSession(
                session_id=sid, title=result[0], engine=engine, space=space,
                client_id=client.client_id,
            ))

        self.app.push_screen(RenameDialog("Rename session", title), renamed)

    async def delete_selected(self, *, confirm):
        node = self.query_one(SessionTree).cursor_node
        client = self.app.client
        if not node or not node.data or node.data[0] != "session":
            client.notice = "Select a session to delete, not a folder"
            return
        sid = node.data[1]
        row = client.visible_catalog().get(sid)
        if row is None:
            client.notice = "Session no longer exists; deletion cancelled"
            return
        engine, space = client.scope

        async def deleted(confirmed):
            if not confirmed:
                return
            current = client.workspace.catalog.get(sid)
            if current is None or (
                current.get("engine", engine), current.get("space", "code")
            ) != (engine, space):
                client.notice = "Session changed or disappeared; deletion cancelled"
                return
            command = (CloseBtw(sid=sid, client_id=client.client_id)
                       if sid.startswith("btw-") else DeleteSession(
                           session_id=sid, engine=engine, space=space,
                           client_id=client.client_id,
                       ))
            await client.execute_action(command)
            # Wait for an authoritative catalog or BTW closure response;
            # the server may reject the command. Never remove optimistically.

        if confirm:
            title = row.get("summary") or row.get("first_prompt") or sid
            self.app.push_screen(
                DeleteDialog(sid, title, row.get("cwd") or "",
                             side_chat=sid.startswith("btw-"), archive=(
                    engine == "codex" and space == "code"
                    and not sid.startswith("btw-")
                    and row.get("tag") != "archived"
                )), deleted
            )
        else:
            await deleted(True)

    def move_selection(self, down: bool) -> None:
        tree = self.query_one(SessionTree)
        (tree.action_cursor_down if down else tree.action_cursor_up)()

    def refresh_catalog(self) -> None:
        client = self.app.client
        tree = self.query_one(SessionTree)
        search = self.query_one(TreeSearch)
        if self.scope != client.scope:
            if self.scope and not search.value:
                self.expanded[self.scope] = expanded_folders(tree)
            self.scope = client.scope
            search.value = ""
        rows = client.visible_catalog()
        signature = (
            client.scope,
            search.value,
            tuple(
                (
                    sid,
                    row.get("cwd"),
                    row.get("summary"),
                    row.get("first_prompt"),
                    row.get("tag"),
                )
                for sid, row in rows.items()
            ),
        )
        if signature == self.signature:
            return
        previous = tree.cursor_node.data if tree.cursor_node else None
        if (
            self.signature
            and self.signature[0] == client.scope
            and not self.signature[1]
        ):
            self.expanded[client.scope] = expanded_folders(tree)
        opened = self.expanded.get(client.scope)
        self.signature = signature
        tree.clear()
        tree.root.expand()
        groups = {}
        archive_root = None
        matches = []
        terms = search.value.casefold().split()
        # Stable partition: normal folders first, then one virtual archive.
        for sid, row in sorted(rows.items(), key=lambda item:
                               item[1].get("tag") == "archived"):
            cwd = row.get("cwd") or "(no directory)"
            title = row.get("summary") or row.get("first_prompt") or sid
            archived = row.get("tag") == "archived"
            if not all(
                t in f"{cwd} {title} {sid} "
                f"{'archived' if archived else ''}".casefold() for t in terms
            ):
                continue
            parent = tree.root
            if archived:
                if archive_root is None:
                    archive_root = tree.root.add(
                        Text("Archived", style="dim"), ("archive", ""),
                        expand=bool(terms) or (
                            opened is not None and ("archive", "") in opened
                        ),
                    )
                parent = archive_root
            key = ("archived_folder" if archived else "folder", cwd)
            if key not in groups:
                expand = bool(terms) or (
                    key in opened
                    if opened is not None
                    else not archived
                    and cwd == rows.get(client.attached_sid, {}).get("cwd")
                )
                groups[key] = parent.add(
                    Text(_safe_remote_text(PurePosixPath(cwd).name or cwd)),
                    key,
                    expand=expand,
                )
            node = groups[key].add_leaf(
                Text(" ".join(_safe_remote_text(title).split())),
                ("session", sid),
            )
            matches.append(node)
        candidates = [*groups.values(), *matches]
        if archive_root is not None:
            candidates.append(archive_root)
        target = next((n for n in candidates if n.data == previous), None)
        if terms and (target is None or target.data[0] != "session"):
            target = matches[0] if matches else None
        if target is None:
            target = next(
                (n for n in matches if n.data[1] == client.attached_sid), None
            )
        parent = target.parent if target else None
        while parent and parent is not tree.root:
            if not parent.is_expanded:
                target = parent
            parent = parent.parent
        # Materialize the new visible lines before using a node's line index.
        # Newly added nodes otherwise still point at the default folder row.
        tree.get_node_at_line(0)
        tree.move_cursor(target or next(iter(tree.root.children), tree.root))
        self.query_one("#tree-heading", Static).update(
            f"{client.engine.title()} / {client.space.title()} · {len(matches)} sessions"
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self.refresh_catalog()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        await self.open_search_result()

    async def open_search_result(self):
        node = self.query_one(SessionTree).cursor_node
        if (
            node
            and node.data
            and node.data[0] == "session"
            and node.data[1] in self.app.client.visible_catalog()
        ):
            await self.app.attach_session(node.data[1])

    async def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        event.stop()
        if event.node.data and event.node.data[0] == "session":
            # Revalidate against the current catalog after a live deletion/move.
            if event.node.data[1] in self.app.client.visible_catalog():
                await self.app.attach_session(event.node.data[1])
        elif event.node.data:
            event.node.toggle()
