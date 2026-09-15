"""Late directory results cannot address controls removed during teardown."""

import asyncio

import pytest

from cc_remote.tui_directories import DirectoryPicker
from tests.test_tui_parity import app_client


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["filter", "scan", "select"])
@pytest.mark.parametrize("fails", [False, True])
async def test_directory_reply_during_teardown_is_discarded(
    monkeypatch, operation, fails,
):
    app, client = app_client()

    async def listing(*args, **kwargs):
        return {"path": "/root", "dirs": []}

    async def rank(paths, query):
        return paths

    monkeypatch.setattr(client, "list_directories", listing)
    monkeypatch.setattr("cc_remote.tui_directories.fuzzy_directories", rank)
    async with app.run_test() as pilot:
        picker = DirectoryPicker(client, "/root")
        app.push_screen(picker)
        await pilot.pause(0.2)
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args, **kwargs):
            started.set()
            await release.wait()
            if fails:
                raise OSError("late failure")
            return ["/root"] if operation == "filter" else {
                "path": "/root", "dirs": [],
            }

        if operation == "filter":
            monkeypatch.setattr(
                "cc_remote.tui_directories.fuzzy_directories", delayed,
            )
            coroutine = picker.apply_filter("", picker.filter_revision)
        else:
            monkeypatch.setattr(client, "list_directories", delayed)
            coroutine = (picker.scan("/root", picker.load_revision, True)
                         if operation == "scan" else picker.select("/root"))
        task = asyncio.create_task(coroutine)
        try:
            await asyncio.wait_for(started.wait(), 1)
            # Textual marks pruning nodes non-displayed before removing their
            # children; the parent's Unmount/cancellation can arrive later.
            picker._pruning = True
            await picker.query_one(".tui-panel").remove()
            release.set()
            await asyncio.wait_for(task, 1)
            assert app.screen is picker
        finally:
            picker._pruning = False
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
