"""Native agents populate the dock without a model call or lifecycle mutation."""
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cc_remote.wrapper.codex_agent_activity import CodexAgentActivity
from tests.test_codex_agents import rollout, task
from tests.test_codex_task_activity import task as detached_task
from tests.test_multisession import _mk_ctx, _mk_machine


def catalog(home, sid, parent=None, rows=(), **kwargs):
    path = rollout(home, sid, parent, rows=rows, **kwargs)
    source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}} if parent else "cli"
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT)")
        db.execute("INSERT OR REPLACE INTO threads VALUES (?,?,?)",
                   (sid, str(path), json.dumps(source)))
    return path


def boundary(sid):
    return "event_msg", {"type": "thread_settings_applied", "thread_id": sid}


def terminal(turn, kind="task_complete"):
    return "event_msg", {"type": kind, "turn_id": turn}


def append(path, *rows):
    with path.open("a") as stream:
        for typ, payload in rows:
            stream.write(json.dumps({"type": typ, "payload": payload}) + "\n")


def compress_catalog(home, sid, path):
    from cc_remote.wrapper.rollout_io import zstd
    target = home / "archived_sessions" / f"{sid}.jsonl.zst"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(zstd.compress(path.read_bytes()))
    path.unlink()
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(target), sid))
    return target


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("compressed_parent", [False, True])
def test_compressed_completed_agents_do_not_reappear_in_background_dock(tmp_path, monkeypatch, legacy, compressed_parent):
    from cc_remote.wrapper import codex_agent_activity as module
    parent = catalog(tmp_path, "parent", rows=[task("copied"), terminal("copied")])
    if compressed_parent:
        compress_catalog(tmp_path, "parent", parent)
    reader = CodexAgentActivity()
    for index in range(11):
        sid = f"child-{index}"
        rows = [task("copied"), terminal("copied")]
        if not legacy:
            rows.append(boundary(sid))
        rows.extend([task(sid), terminal(sid)])
        path = catalog(tmp_path, sid, "parent", rows=rows)
        compress_catalog(tmp_path, sid, path)
    assert reader.read(tmp_path, {"parent"})["parent"] == []
    # Eleven descendants exceed the shared decoded-file cache. Unchanged
    # completed records must not be decompressed every background refresh.
    monkeypatch.setattr(module, "rollout_open", lambda *a, **k: pytest.fail("unchanged rollout reread"))
    assert reader.read(tmp_path, {"parent"})["parent"] == []


def test_compressed_agent_still_running_and_later_replacement(tmp_path):
    from cc_remote.wrapper.rollout_io import zstd
    catalog(tmp_path, "parent")
    child = catalog(tmp_path, "child", "parent", rows=[boundary("child"), task("own")])
    source = child.read_bytes()
    compressed = compress_catalog(tmp_path, "child", child)
    reader = CodexAgentActivity()
    assert reader.read(tmp_path, {"parent"})["parent"][0].status == "running"
    ending = json.dumps({"type": "event_msg", "payload": terminal("own")[1]}).encode() + b"\n"
    compressed.write_bytes(zstd.compress(source + ending))
    assert reader.read(tmp_path, {"parent"})["parent"] == []
    compressed.write_bytes(b"invalid compressed record")
    assert reader.read(tmp_path, {"parent"})["parent"][0].status == "unknown"


@pytest.mark.parametrize("completion_kind", ["task_complete", "task_completed"])
def test_native_lifecycle_ignores_inherited_turn_and_unrelated_terminals(tmp_path, completion_kind):
    catalog(tmp_path, "parent", rows=[task("parent-turn")])
    child = catalog(tmp_path, "child", "parent", rows=[
        task("parent-turn"), boundary("child"), task("child-turn"),
        terminal("parent-turn", completion_kind), terminal("other-turn", completion_kind),
    ])
    reader = CodexAgentActivity()
    initial = reader.read(tmp_path, {"parent"})["parent"]
    assert len(initial) == 1
    assert initial[0].item_id == "codex-agent:child"
    assert initial[0].kind == "agent" and initial[0].status == "running"
    assert initial[0].title == "/root/child"
    assert reader.read(tmp_path, {"parent"})["parent"] == initial
    assert CodexAgentActivity().read(tmp_path, {"parent"})["parent"] == initial
    append(child, terminal("child-turn", completion_kind))
    assert reader.read(tmp_path, {"parent"})["parent"] == []
    assert CodexAgentActivity().read(tmp_path, {"parent"})["parent"] == []
    append(child, task("followup"))
    assert reader.read(tmp_path, {"parent"})["parent"][0].status == "running"
    append(child, terminal("child-turn", completion_kind))  # Delayed old receipt is not a new finish.
    assert reader.read(tmp_path, {"parent"})["parent"][0].status == "running"
    append(child, terminal("followup", "turn_aborted"))
    assert reader.read(tmp_path, {"parent"})["parent"] == []


@pytest.mark.parametrize("completion_kind", ["task_complete", "task_completed"])
def test_legacy_native_agent_excludes_parent_context(tmp_path, completion_kind):
    catalog(tmp_path, "parent", rows=[task("inherited")])
    child = catalog(tmp_path, "child", "parent", rows=[
        task("inherited"), task("own"), terminal("inherited", completion_kind)])
    reader = CodexAgentActivity()
    assert reader.read(tmp_path, {"parent"})["parent"][0].status == "running"
    append(child, terminal("own", completion_kind))
    assert reader.read(tmp_path, {"parent"})["parent"] == []
    assert CodexAgentActivity().read(tmp_path, {"parent"})["parent"] == []


def test_only_direct_agents_in_same_account_and_not_ordinary_forks(tmp_path):
    home, other = tmp_path / "account", tmp_path / "other"
    catalog(home, "parent")
    catalog(home, "child", "parent", rows=[boundary("child"), task("own")])
    catalog(home, "nested", "child", rows=[boundary("nested"), task("nested")])
    catalog(home, "sibling", "someone", rows=[boundary("sibling"), task("sibling")])
    catalog(other, "foreign", "parent", rows=[boundary("foreign"), task("foreign")])
    catalog(home, "ordinary", rows=[task("user-fork")])
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("UPDATE threads SET source=? WHERE id='ordinary'",
                   (json.dumps({"forked_from_id": "parent"}),))
    data = CodexAgentActivity().read(home, {"parent", "child"})
    assert [item.item_id for item in data["parent"]] == ["codex-agent:child"]
    assert [item.item_id for item in data["child"]] == ["codex-agent:nested"]


def test_foreign_catalog_path_and_changed_identity_never_report_live(tmp_path):
    home, other = tmp_path / "account", tmp_path / "other"
    catalog(home, "parent")
    local = catalog(home, "child", "parent", rows=[boundary("child"), task("own")])
    foreign = catalog(other, "child", "parent", rows=[boundary("child"), task("secret")])
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("UPDATE threads SET rollout_path=? WHERE id='child'", (str(foreign),))
    reader = CodexAgentActivity()
    assert reader.read(home, {"parent"})["parent"][0].status == "unknown"
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("UPDATE threads SET rollout_path=? WHERE id='child'", (str(local),))
    assert reader.read(home, {"parent"})["parent"][0].status == "running"
    local.write_text(local.read_text().replace('"parent_thread_id": "parent"', '"parent_thread_id": "other"'))
    assert reader.read(home, {"parent"})["parent"][0].status == "unknown"


def test_stale_catalog_without_child_source_does_not_create_a_background_task(tmp_path):
    catalog(tmp_path, "parent")
    child = catalog(tmp_path, "child", "parent", rows=[boundary("child"), task("own")])
    contents = child.read_bytes()
    child.unlink()
    reader = CodexAgentActivity()
    assert reader.read(tmp_path, {"parent"})["parent"] == []
    assert reader.read(tmp_path, {"parent"})["parent"] == []
    # A delayed native writer can still materialize a genuinely active child.
    child.write_bytes(contents)
    assert reader.read(tmp_path, {"parent"})["parent"][0].status == "running"
    child.unlink()
    assert reader.read(tmp_path, {"parent"})["parent"][0].status == "unknown"


def test_settled_child_removed_from_disk_does_not_reappear_as_unknown(tmp_path):
    catalog(tmp_path, "parent")
    child = catalog(tmp_path, "child", "parent", rows=[
        boundary("child"), task("own"), terminal("own")])
    reader = CodexAgentActivity()
    assert reader.read(tmp_path, {"parent"})["parent"] == []
    child.unlink()
    assert reader.read(tmp_path, {"parent"})["parent"] == []


def test_partial_record_missing_source_and_catalog_failure_are_not_completion(tmp_path):
    catalog(tmp_path, "parent")
    child = catalog(tmp_path, "child", "parent", rows=[boundary("child"), task("own")])
    reader = CodexAgentActivity()
    assert reader.read(tmp_path, {"parent"})["parent"][0].status == "running"
    ending = json.dumps({"type": "event_msg", "payload": terminal("own")[1]})
    with child.open("a") as stream:
        stream.write(ending[:20])
    assert reader.read(tmp_path, {"parent"})["parent"][0].status == "unknown"
    with child.open("a") as stream:
        stream.write(ending[20:] + "\n")
    assert reader.read(tmp_path, {"parent"})["parent"] == []
    append(child, task("next"))
    assert reader.read(tmp_path, {"parent"})["parent"][0].status == "running"
    child.unlink()
    assert reader.read(tmp_path, {"parent"})["parent"][0].status == "unknown"
    with sqlite3.connect(tmp_path / "state_5.sqlite") as db:
        db.execute("DELETE FROM threads WHERE id='child'")
    assert reader.read(tmp_path, {"parent"})["parent"][0].status == "unknown"
    (tmp_path / "state_5.sqlite").write_bytes(b"corrupt")
    with pytest.raises(sqlite3.DatabaseError):
        reader.read(tmp_path, {"parent"})


def test_incremental_budget_and_rewrite_do_not_reuse_old_terminal(tmp_path, monkeypatch):
    from cc_remote.wrapper import codex_agent_activity as module
    catalog(tmp_path, "parent")
    rows = [task("copied"), terminal("copied"), boundary("child"), task("own"), terminal("own")]
    child = catalog(tmp_path, "child", "parent", rows=rows)
    monkeypatch.setattr(module, "_READ_BUDGET", 1)
    reader = CodexAgentActivity()
    for _ in range(4):
        assert reader.read(tmp_path, {"parent"})["parent"] == []
    assert reader.read(tmp_path, {"parent"})["parent"] == []
    # Replace in place with the same native identity, now a new live task.
    child = catalog(tmp_path, "child", "parent", rows=[boundary("child"), task("new")])
    assert reader.read(tmp_path, {"parent"})["parent"] == []
    assert reader.read(tmp_path, {"parent"})["parent"][0].status == "running"
    assert reader._cursors["child"].offset == child.stat().st_size
    append(child, task("later"), terminal("later"))
    assert reader.read(tmp_path, {"parent"})["parent"][0].status == "unknown"
    assert reader.read(tmp_path, {"parent"})["parent"] == []


def test_cold_legacy_scan_never_introduces_old_tasks_while_parent_is_paged(tmp_path, monkeypatch):
    from cc_remote.wrapper import codex_agent_activity as module
    catalog(tmp_path, "parent", rows=[task(f"parent-{i}") for i in range(12)])
    for index in range(11):
        sid = f"child-{index}"
        path = catalog(tmp_path, sid, "parent", rows=[task(sid), terminal(sid)])
        compress_catalog(tmp_path, sid, path)
    monkeypatch.setattr(module, "_READ_BUDGET", 200)
    reader = CodexAgentActivity()
    for _ in range(30):
        assert reader.read(tmp_path, {"parent"})["parent"] == []
    assert all(cursor.complete for cursor in reader._cursors.values())


def test_no_catalog_has_no_filesystem_side_effects(tmp_path):
    assert CodexAgentActivity().read(tmp_path, {"parent"}) == {"parent": []}
    assert list(tmp_path.iterdir()) == []


def test_native_catalog_limit_is_per_parent(tmp_path, monkeypatch):
    from cc_remote.wrapper import codex_agent_activity as module

    monkeypatch.setattr(module, "_MAX_CHILDREN", 2)
    for sid in ("first-old", "first-recent"):
        catalog(tmp_path, sid, "first", rows=[boundary(sid), task(sid), terminal(sid)])
    child = catalog(tmp_path, "second-child", "second", rows=[
        boundary("second-child"), task("own")])
    reader = CodexAgentActivity()
    data = reader.read(tmp_path, {"first", "second"})
    assert data["first"] == []
    assert [item.item_id for item in data["second"]] == ["codex-agent:second-child"]
    assert data["second"][0].status == "running"

    append(child, terminal("own"))
    catalog(tmp_path, "second-new", "second", rows=[boundary("second-new"), task("new")])
    data = reader.read(tmp_path, {"first", "second"})
    assert data["first"] == []
    assert [item.item_id for item in data["second"]] == ["codex-agent:second-new"]
    assert data["second"][0].status == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("warm", [False, True])
async def test_oversized_parent_does_not_block_other_sessions(tmp_path, monkeypatch, warm):
    from cc_remote.wrapper import codex_agent_activity as module

    monkeypatch.setattr(module, "_MAX_CHILDREN", 2)
    first, second = sorted([str(uuid4()), str(uuid4())])
    child = catalog(tmp_path, "first-child", first, rows=[boundary("first-child"), task("first")])
    other = catalog(tmp_path, "second-child", second, rows=[boundary("second-child"), task("second")])
    machine, transport = _mk_machine()
    contexts = [_mk_ctx(sid, sid) for sid in (first, second)]
    for ctx in contexts:
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
    monkeypatch.setattr(machine, "_codex_profile_for_ctx", lambda _: SimpleNamespace(home=tmp_path))
    if warm:
        await machine._refresh_codex_background_processes()
    for sid in ("first-old", "first-older"):
        catalog(tmp_path, sid, first, rows=[boundary(sid), task(sid), terminal(sid)])
    append(child, terminal("first"))
    append(other, terminal("second"))
    latest = catalog(tmp_path, "second-new", second, rows=[boundary("second-new"), task("new")])
    transport.sent.clear()

    await machine._refresh_codex_background_processes()
    assert set(contexts[1].codex_background_processes) == {"codex-agent:second-new"}
    assert contexts[1].codex_background_processes["codex-agent:second-new"].status == "running"
    assert any(event.sid == second for event in transport.sent)
    retained = contexts[0].codex_background_processes
    assert set(retained) == ({"codex-agent:first-child"} if warm else set())
    if warm:
        assert retained["codex-agent:first-child"].status == "unknown"
    transport.sent.clear()
    await machine._refresh_codex_background_processes()
    assert transport.sent == [], "an overflowing parent must not cause repeated broadcasts"

    # Returning below the cap permits reading the retained child's real terminal.
    with sqlite3.connect(tmp_path / "state_5.sqlite") as db:
        db.execute("DELETE FROM threads WHERE id IN ('first-old', 'first-older')")
    append(latest, terminal("new"))
    await machine._refresh_codex_background_processes()
    assert all(ctx.codex_background_processes == {} and ctx.state == "idle" for ctx in contexts)
    assert {event.sid for event in transport.sent} == ({first, second} if warm else {second})


@pytest.mark.asyncio
async def test_wrapper_merges_native_and_mcp_and_reseeds_after_parent_completion(tmp_path, monkeypatch):
    from cc_remote.protocol import Hello
    parent = str(uuid4())
    catalog(tmp_path, parent, rows=[task("main"), terminal("main")])
    child = catalog(tmp_path, "child", parent, rows=[boundary("child"), task("own")])
    store, key = detached_task(tmp_path, parent)
    machine, transport = _mk_machine()
    ctx, other = _mk_ctx(parent, parent), _mk_ctx("other", "other")
    for current in (ctx, other):
        current.engine = "codex"
        current.state = "idle"
        machine.sessions[current.key] = current
    monkeypatch.setattr(machine, "_codex_profile_for_ctx", lambda _: SimpleNamespace(home=tmp_path))
    await machine._refresh_codex_background_processes()
    frames = [m for m in transport.sent if m.type == "background_process_sync"]
    assert len(frames) == 1 and frames[0].sid == parent
    assert {item.item_id for item in frames[0].items} == {f"async-task:{key}", "codex-agent:child"}
    assert ctx.state == "idle" and other.codex_background_processes == {}
    transport.sent.clear()
    await machine._refresh_codex_background_processes()
    assert transport.sent == []
    await machine._handle_client_hello(Hello(
        role="client", client_id="fresh", route_id="route",
        cursors={parent: ctx.buffer.tail_seq}, generations={parent: machine.instance_id}))
    seeds = [m for m in transport.sent if m.type == "background_process_sync"
             and m.sid == parent and m.to == "fresh"]
    assert len(seeds) == 1 and len(seeds[0].items) == 2
    append(child, terminal("own"))
    await machine._refresh_codex_background_processes()
    assert list(ctx.codex_background_processes) == [f"async-task:{key}"]
    store.update(key, state="completed", delivery="delivered")
    await machine._refresh_codex_background_processes()
    assert ctx.codex_background_processes == {} and ctx.state == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("warm", [False, True])
async def test_bad_native_catalog_does_not_block_mcp_refresh(tmp_path, monkeypatch, warm):
    parent = str(uuid4())
    catalog(tmp_path, parent)
    child = catalog(tmp_path, "child", parent, rows=[boundary("child"), task("own")])
    machine, transport = _mk_machine()
    ctx = _mk_ctx(parent, parent)
    ctx.engine = "codex"
    machine.sessions[ctx.key] = ctx
    monkeypatch.setattr(machine, "_codex_profile_for_ctx", lambda _: SimpleNamespace(home=tmp_path))
    if warm:
        await machine._refresh_codex_background_processes()
    retained = dict(ctx.codex_background_processes or {})
    database = tmp_path / "state_5.sqlite"
    original = database.read_bytes()
    database.write_bytes(b"corrupt")
    store, key = detached_task(tmp_path, parent)
    transport.sent.clear()

    await machine._refresh_codex_background_processes()
    assert set(ctx.codex_background_processes) == set(retained) | {f"async-task:{key}"}
    assert all(ctx.codex_background_processes[key] == item for key, item in retained.items())
    assert len(transport.sent) == 1 and transport.sent[0].sid == parent
    transport.sent.clear()
    await machine._refresh_codex_background_processes()
    assert transport.sent == [], "a persistent catalog error must not rebroadcast unchanged tasks"

    store.update(key, state="completed", delivery="delivered")
    await machine._refresh_codex_background_processes()
    assert ctx.codex_background_processes == retained
    assert len(transport.sent) == 1
    assert {item.item_id for item in transport.sent[0].items} == set(retained)
    transport.sent.clear()
    database.write_bytes(original)
    append(child, terminal("own"))
    await machine._refresh_codex_background_processes()
    assert ctx.codex_background_processes == {} and ctx.state == "idle"
    assert len(transport.sent) == int(warm)


@pytest.mark.asyncio
async def test_bad_mcp_snapshot_does_not_block_native_agent_refresh(tmp_path, monkeypatch):
    from cc_remote.wrapper import codex_tasks

    parent = str(uuid4())
    catalog(tmp_path, parent)
    store, key = detached_task(tmp_path, parent)
    machine, transport = _mk_machine()
    ctx = _mk_ctx(parent, parent)
    ctx.engine = "codex"
    machine.sessions[ctx.key] = ctx
    monkeypatch.setattr(machine, "_codex_profile_for_ctx", lambda _: SimpleNamespace(home=tmp_path))
    await machine._refresh_codex_background_processes()
    retained = dict(ctx.codex_background_processes)
    original = codex_tasks.read_task_activity

    def unavailable(_):
        raise OSError("task state unavailable")

    monkeypatch.setattr(codex_tasks, "read_task_activity", unavailable)
    child = catalog(tmp_path, "child", parent, rows=[boundary("child"), task("own")])
    transport.sent.clear()
    await machine._refresh_codex_background_processes()
    assert set(ctx.codex_background_processes) == set(retained) | {"codex-agent:child"}
    assert ctx.codex_background_processes[f"async-task:{key}"] == retained[f"async-task:{key}"]
    assert len(transport.sent) == 1 and transport.sent[0].sid == parent
    transport.sent.clear()
    await machine._refresh_codex_background_processes()
    assert transport.sent == []

    append(child, terminal("own"))
    await machine._refresh_codex_background_processes()
    assert ctx.codex_background_processes == retained
    assert len(transport.sent) == 1
    transport.sent.clear()
    monkeypatch.setattr(codex_tasks, "read_task_activity", original)
    store.update(key, state="completed", delivery="delivered")
    await machine._refresh_codex_background_processes()
    assert ctx.codex_background_processes == {} and ctx.state == "idle"
    assert len(transport.sent) == 1 and transport.sent[0].items == []


@pytest.mark.asyncio
async def test_partial_refresh_retains_other_resident_agent_until_its_terminal(tmp_path, monkeypatch):
    first, second = str(uuid4()), str(uuid4())
    catalog(tmp_path, first)
    catalog(tmp_path, second)
    catalog(tmp_path, "first-child", first, rows=[boundary("first-child"), task("first-turn")])
    child = catalog(tmp_path, "second-child", second, rows=[
        boundary("second-child"), task("second-turn")])
    machine, transport = _mk_machine()
    contexts = [_mk_ctx(sid, sid) for sid in (first, second)]
    for ctx in contexts:
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
    monkeypatch.setattr(machine, "_codex_profile_for_ctx", lambda _: SimpleNamespace(home=tmp_path))
    await machine._refresh_codex_background_processes()
    transport.sent.clear()

    # Focus/BTW synchronization refreshes only the first context's public level.
    await machine._refresh_codex_background_processes([contexts[0]])
    assert transport.sent == []
    with sqlite3.connect(tmp_path / "state_5.sqlite") as db:
        db.execute("DELETE FROM threads WHERE id='second-child'")
    await machine._refresh_codex_background_processes()
    retained = contexts[1].codex_background_processes
    assert set(retained) == {"codex-agent:second-child"}
    assert retained["codex-agent:second-child"].status == "unknown"
    assert len(transport.sent) == 1 and transport.sent[0].sid == second

    # A partial refresh must not broadcast changes for the unfocused session.
    catalog(tmp_path, "second-child", second, rows=[
        boundary("second-child"), task("second-turn")])
    append(child, terminal("second-turn"))
    transport.sent.clear()
    await machine._refresh_codex_background_processes([contexts[0]])
    assert transport.sent == [] and contexts[1].codex_background_processes == retained
    await machine._refresh_codex_background_processes()
    assert contexts[1].codex_background_processes == {}
    assert len(transport.sent) == 1 and transport.sent[0].items == []
    assert all(ctx.state == "idle" for ctx in contexts)

    # Removed residents must not accumulate indefinitely in the account cache.
    del machine.sessions[second]
    await machine._refresh_codex_background_processes([contexts[0]])
    reader = machine._codex_agent_activity[tmp_path]
    assert second not in reader._previous
    assert "second-child" not in reader._cursors
