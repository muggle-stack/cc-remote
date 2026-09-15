"""fzf matching over wrapper-owned directory listings, without a shell."""

import asyncio
import os
import shutil
from collections import deque
from functools import partial

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from cc_remote.tui import _safe_remote_text
from cc_remote.tui_modal import ModalEditor, PickerList, hints
from cc_remote.tui_panels import ActionPicker


async def fuzzy_directories(paths: list[str], query: str) -> list[str]:
    """Use fzf's real ranking while retaining the workspace's Vim key layers."""
    binary = shutil.which("fzf")
    if not binary:
        raise ValueError(
            "fzf is required for directory selection; install fzf first"
        )
    # Personal fzf bindings/default commands may execute programs. This picker
    # only filters a supplied, NUL-delimited list and never executes shell code.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("FZF_")
    }
    process = await asyncio.create_subprocess_exec(
        binary,
        "--read0",
        "--print0",
        "--filter=" + query,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        output, _ = await asyncio.wait_for(
            process.communicate("\0".join(paths).encode() + b"\0"),
            timeout=3,
        )
        if process.returncode not in (0, 1):
            raise ValueError("fzf could not filter directories")
        allowed = set(paths)
        return [path for path in output.decode().split("\0") if path in allowed]
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


class DirectoryPicker(ActionPicker):
    """Recursively browse bounded, fresh wrapper listings while filtering."""

    MAX_READS = 256
    MAX_PATHS = 8192
    REFRESH_SECONDS = 5

    local_actions = {
        "cancel",
        "search",
        "edit",
        "down",
        "up",
        "parent",
        "open",
        "refresh",
    }

    def __init__(self, client, path: str):
        super().__init__(client, None)
        self.path = path
        self.parent_path = None
        self.paths = []
        self.matches = []
        self.loading = False
        self.load_revision = 0
        self.filter_revision = 0
        self.filter_worker = None
        self.load_worker = None
        self.select_worker = None
        self.matched_query = None
        self.selected_match = None
        self.scanning = False
        self.scan_note = ""

    def compose(self):
        with Vertical(classes="tui-panel"):
            yield Label(
                "Directory · fzf · "
                + self.client.keys.layer_label("picker", "choose")
                + " selects; "
                + self.client.keys.layer_label("picker", "parent") + "/"
                + self.client.keys.layer_label("picker", "open")
                + " browses", markup=False
            )
            yield Label(self.path, id="directory-path", markup=False)
            yield ModalEditor(
                classes="search-editor",
                id="search",
                placeholder="Fuzzy search directories",
            )
            yield PickerList()
            yield Static("", id="directory-status", markup=False)
            yield hints()

    def on_mount(self):
        self.action_search()
        self.start_load(self.path, refresh=True)
        self.set_interval(self.REFRESH_SECONDS, self.refresh_if_idle)

    def refresh_if_idle(self):
        if self.app.screen is self and not self.scanning and not self.loading:
            self.start_load(self.path, refresh=True, preserve=True)

    def filter(self, value):
        if not self.is_mounted or not self.display:
            return
        self.filter_revision += 1
        revision = self.filter_revision
        if self.filter_worker:
            self.filter_worker.cancel()
        listing = self.query_one(OptionList)
        if self.matches and listing.highlighted is not None:
            self.selected_match = self.matches[listing.highlighted]
        # Retain selection while fresh listings arrive for the same query.
        # After typing, stale highlighted matches must never be selectable.
        if value != self.matched_query:
            listing.clear_options()
            self.matches = []
            self.selected_match = None
        self.filter_worker = self.run_worker(
            partial(self.apply_filter, value, revision)
        )

    async def apply_filter(self, query, revision):
        await asyncio.sleep(0.04)
        if not self.is_mounted or not self.display:
            return
        try:
            matches = await fuzzy_directories(self.paths, query)
        except (ValueError, OSError, TimeoutError) as exc:
            if (revision == self.filter_revision
                    and self.is_mounted and self.display):
                self.query_one("#directory-status", Static).update(str(exc))
            return
        if (revision != self.filter_revision or self.loading
                or not self.is_mounted or not self.display):
            return
        self.matches = matches
        self.matched_query = query
        listing = self.query_one(OptionList)
        listing.clear_options()
        for index, path in enumerate(matches):
            label = path + (
                "  [current directory]" if path == self.path else ""
            )
            listing.add_option(
                Option(Text(_safe_remote_text(label)), id=str(index))
            )
        listing.highlighted = (
            matches.index(self.selected_match) if self.selected_match in matches
            else (0 if matches else None)
        )
        self.query_one("#directory-status", Static).update(
            f"{len(matches)} matches · recursive · {self.scan_note}"
        )

    def start_load(self, path, *, refresh=True, preserve=False):
        # Called from Mount before Textual sets is_mounted.
        if not self.display:
            return
        self.loading = not preserve
        self.load_revision += 1
        self.filter_revision += 1
        if self.filter_worker:
            self.filter_worker.cancel()
        if self.load_worker:
            self.load_worker.cancel()
        if not preserve:
            self.matches = []
            self.query_one(OptionList).clear_options()
            self.query_one(ModalEditor).load_text("")
        self.query_one("#directory-status", Static).update(
            "Reading wrapper directories…"
        )
        self.load_worker = self.run_worker(
            partial(self.load, path, self.load_revision, refresh)
        )

    async def load(self, path, revision, refresh):
        self.scanning = True
        try:
            await self.scan(path, revision, refresh)
        finally:
            if revision == self.load_revision:
                self.scanning = False

    async def scan(self, path, revision, refresh):
        try:
            result = await self.client.list_directories(path, refresh=refresh)
        except (ValueError, OSError) as exc:
            if (revision == self.load_revision
                    and self.is_mounted and self.display):
                self.loading = False
                self.query_one("#directory-status", Static).update(
                    _safe_remote_text(str(exc))
                )
            return
        if (revision != self.load_revision
                or not self.is_mounted or not self.display):
            return
        self.path, self.parent_path = result["path"], result.get("parent")
        candidates = [
            self.path,
            *(row.get("path") for row in result.get("dirs", [])),
        ]
        candidates += [
            row.get("cwd") for row in self.client.workspace.catalog.values()
        ]
        self.paths = list(
            dict.fromkeys(
                path
                for path in candidates
                if isinstance(path, str)
                and path.startswith("/")
                and "\0" not in path
            )
        )[:self.MAX_PATHS]
        self.loading = False
        self.query_one("#directory-path", Label).update(
            _safe_remote_text(self.path)
        )
        pending = deque(row.get("path") for row in result.get("dirs", []))
        visited = {self.path}
        requested = {self.path}
        count = 1
        self.scan_note = "scanning…"
        self.filter(self.query_one(ModalEditor).text.replace("\n", " "))
        # Resolve each listing on the wrapper. Never walk the TUI host, escape
        # the selected subtree through symlinks, or launch unbounded RPCs.
        async def read(child):
            try:
                return await self.client.list_directories(child, refresh=True)
            except (ValueError, OSError):
                return None

        while (pending and count < self.MAX_READS
               and len(self.paths) < self.MAX_PATHS):
            batch = []
            while pending and len(batch) < min(4, self.MAX_READS - count):
                child = pending.popleft()
                if (not isinstance(child, str) or child in requested
                        or not child.startswith(self.path.rstrip("/") + "/")):
                    continue
                requested.add(child)
                batch.append(child)
            if not batch:
                continue
            count += len(batch)
            pages = await asyncio.gather(*(read(child) for child in batch))
            if (revision != self.load_revision
                    or not self.is_mounted or not self.display):
                return
            for page in pages:
                if len(self.paths) >= self.MAX_PATHS:
                    break
                if not page:
                    continue
                canonical = page["path"]
                if (canonical in visited or not canonical.startswith(
                        self.path.rstrip("/") + "/")):
                    continue
                visited.add(canonical)
                for row in page.get("dirs", []):
                    child = row.get("path")
                    if (isinstance(child, str) and "\0" not in child
                            and child.startswith(self.path.rstrip("/") + "/")
                            and child not in self.paths):
                        self.paths.append(child)
                        pending.append(child)
                        if len(self.paths) >= self.MAX_PATHS:
                            break
            # Publish batches without waiting for slow/unreadable descendants.
            self.filter(self.query_one(ModalEditor).text.replace("\n", " "))
            await asyncio.sleep(0.05)
        if not self.is_mounted or not self.display:
            return
        self.scan_note = (
            "scan limit reached; narrow the root directory"
            if pending else "live refresh every 5s"
        )
        self.filter(self.query_one(ModalEditor).text.replace("\n", " "))

    def selected_path(self):
        listing = self.query_one(OptionList)
        query = self.query_one(ModalEditor).text.replace("\n", " ")
        if (
            self.loading
            or listing.highlighted is None
            or query != self.matched_query
        ):
            return None
        item = listing.get_option_at_index(listing.highlighted)
        index = int(item.id)
        return self.matches[index] if index < len(self.matches) else None

    def action_open(self):
        if path := self.selected_path():
            self.start_load(path)

    def action_parent(self):
        if self.parent_path and not self.loading:
            self.start_load(self.parent_path)

    def action_refresh(self):
        self.start_load(self.path, refresh=True, preserve=True)

    def open_form(self, name):
        if path := self.selected_path():
            if self.load_worker:
                self.load_worker.cancel()
            self.loading = True
            self.query_one("#directory-status", Static).update(
                "Checking selected directory…"
            )
            self.select_worker = self.run_worker(partial(self.select, path))

    async def select(self, path):
        try:
            result = await self.client.list_directories(path, refresh=True)
        except (ValueError, OSError) as exc:
            if not self.is_mounted or not self.display:
                return
            self.loading = False
            self.query_one("#directory-status", Static).update(
                _safe_remote_text(str(exc))
            )
            return
        if self.is_mounted and self.display:
            self.dismiss((result["path"],))

    def on_unmount(self):
        self.filter_revision += 1
        self.load_revision += 1
        if self.filter_worker:
            self.filter_worker.cancel()
        if self.load_worker:
            self.load_worker.cancel()
        if self.select_worker:
            self.select_worker.cancel()
