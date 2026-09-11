import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from cc_remote.protocol import (
    ActDshSubagent, ArchiveSession, DownloadDsh, GetFilePreview, ReadDsh,
    Query, is_downstream, serialize, deserialize,
)
from cc_remote.wrapper.dsh_client import DshError
from cc_remote.wrapper.dsh_features import CHUNK_SIZE, path_mention
from tests.test_dsh_runtime import setup_runtime


@pytest.mark.asyncio
async def test_search_read_is_private_and_never_creates_an_agent():
    machine, transport, runtime, client = setup_runtime()
    async def rpc(endpoint, args=None):
        assert endpoint == "session/search"
        assert args == {"request": {"query": "needle"}}
        return {"items": [{"sessionId": "match", "snippet": "context needle"}], "hasMore": True}
    client.rpc = rpc
    cmd = ReadDsh(sid="dsh@root", kind="search", query="needle", cmd_id="read-search", client_id="browser")
    assert deserialize(serialize(cmd)) == cmd
    response = await machine._handle(cmd)
    assert response.items[0].sid == "dsh@match" and response.has_more
    assert response.request_id == cmd.cmd_id and response.to == "browser"
    assert not is_downstream(response) and machine.sessions == {}
    assert not response.error
    await runtime.close()


@pytest.mark.asyncio
async def test_partial_references_preserve_canonical_native_session_mentions():
    machine, _, runtime, client = setup_runtime()
    async def rpc(endpoint, args=None):
        assert args == {"agentId": "root", "query": "abc"}
        if endpoint == "fileReferences/list":
            raise DshError("unavailable", "resolver unavailable")
        return [{"sessionId": "target", "label": "Example", "mention": "@[Example](dsh-session:target)", "cwd": "/tmp"}]
    client.rpc = rpc
    result = await machine._handle(ReadDsh(sid="dsh@root", kind="references", query="abc", cmd_id="read-ref", client_id="browser"))
    assert result.error and result.items[0].mention == "@[Example](dsh-session:target)"
    assert machine.sessions == {}
    await runtime.close()


@pytest.mark.parametrize(("path", "directory", "expected"), [
    ("a.txt", False, "@a.txt"), ("a b.txt", False, '@"a b.txt"'),
    ("a b", True, '@"a b/'), ("src", True, "@src/"),
    ('bad"name', False, None), ("bad\nname", False, None),
])
def test_native_file_mention_grammar(path, directory, expected):
    assert path_mention(path, directory) == expected


@pytest.mark.asyncio
async def test_child_delivery_is_authoritatively_bound_and_unknown_is_not_retried():
    machine, _, runtime, client = setup_runtime()
    calls = []
    async def rpc(endpoint, args=None):
        calls.append((endpoint, args))
        if endpoint == "subagents/list":
            return {"entries": [{"id": "child", "mode": "continuable"}], "parentAvailable": True}
        assert args["request"]["parentSessionId"] == "root"
        assert args["request"]["childSessionId"] == "child"
        assert args["request"]["delivery"] == "steer"
        raise DshError("disconnected", "结果未知", outcome_unknown=True)
    client.rpc = rpc
    result = await machine._handle(ActDshSubagent(sid="dsh@root", target_sid="dsh@foreign", action="queue", prompt="hi", cmd_id="foreign", client_id="browser"))
    assert result.status == "error" and len(calls) == 1
    result = await machine._handle(ActDshSubagent(sid="dsh@root", target_sid="dsh@child", action="steer", prompt="hi", cmd_id="steer", client_id="browser"))
    assert result.status == "unknown" and result.request_id == "steer"
    assert len([call for call in calls if call[0] == "subagents/prompt"]) == 1
    assert not is_downstream(result)
    await runtime.close()


@pytest.mark.asyncio
async def test_descendant_catalog_cannot_read_unrelated_tree():
    machine, _, runtime, client = setup_runtime()
    result = await machine._handle(ReadDsh(sid="dsh@root", target_sid="dsh@foreign", kind="subagents", cmd_id="read-tree", client_id="browser"))
    assert result.error and not result.items
    assert not any(call[0] == "subagents/list" for call in client.calls)
    await runtime.close()


@pytest.mark.asyncio
async def test_diagnostics_only_forward_public_metadata():
    machine, _, runtime, client = setup_runtime()
    async def rpc(endpoint, args=None):
        assert endpoint == "pluginInventory/list"
        return {"entries": [{"entryId": "p", "moduleName": "plugin", "enabled": True, "fiberPhase": "active", "config": {"token": "secret"}}],
                "agentPresets": [{"id": "standard", "broken": "secret broken config"}]}
    client.rpc = rpc
    response = await machine._handle(ReadDsh(sid="dsh@root", kind="diagnostics", cmd_id="read-diag", client_id="browser"))
    assert not response.error and "secret" not in serialize(response)
    await runtime.close()


@pytest.mark.asyncio
async def test_archive_is_native_one_way_busy_safe_and_read_only():
    machine, _, runtime, client = setup_runtime()
    async def listing():
        return [{"sessionId": "root", "running": False, "updatedAt": 100, "cwd": "/tmp", "projections": {"values": {}}}]
    client.list_sessions = listing
    async def rpc(endpoint, args=None):
        assert endpoint == "workspace/archiveSession" and args == {"request": {"sessionId": "root"}}
        return {"archivedSessionIds": ["root"]}
    client.rpc = rpc
    await machine._handle(ArchiveSession(session_id="dsh@root", engine="dsh", archived=True, cmd_id="archive", client_id="browser"))
    assert "root" in runtime.archived
    result = await machine._handle(ArchiveSession(session_id="dsh@root", engine="dsh", archived=False, cmd_id="unarchive", client_id="browser"))
    assert result.code == "dsh_archive_one_way"
    rows = await runtime.handle_list_sessions(SimpleNamespace(cmd_id="list", client_id="browser"))
    assert rows.sessions[0].tag == "archived"
    ctx = await runtime.ctx("dsh@root")
    response = await runtime.query(ctx, Query(sid=ctx.key, prompt="do not run", msg_id="blocked"))
    assert response.code == "dsh_archived"
    assert ctx.sdk.watch is None
    await runtime.close()


@pytest.mark.asyncio
async def test_cold_file_read_uses_snapshot_without_follow(tmp_path):
    machine, _, runtime, client = setup_runtime()
    (tmp_path / "doc.md").write_text("# Document")
    previous = client.history_snapshot
    async def snapshot(sid, **options):
        result = await previous(sid, **options)
        result["header"]["cwd"] = str(tmp_path)
        return result
    client.history_snapshot = snapshot
    response = await machine._handle(GetFilePreview(sid="dsh@cold", path=str(tmp_path), request_id="directory", client_id="browser"))
    assert response.directory and response.to == "browser"
    assert machine.sessions["dsh@cold"].sdk.watch is None
    response = await machine._handle(GetFilePreview(sid="dsh@cold", path="doc.md", request_id="file", client_id="browser"))
    assert not response.directory and response.content == "# Document"
    await runtime.close()


@pytest.mark.asyncio
async def test_export_chunks_are_private_bound_and_removed_on_cancel():
    machine, _, runtime, client = setup_runtime()
    payload = b"PK" + b"x" * CHUNK_SIZE
    async def export(sid, filename, limit):
        assert sid == "dsh@root" and limit > len(payload)
        Path(filename).write_bytes(payload)
        return len(payload)
    client.export_session = export
    first = await machine._handle(DownloadDsh(sid="dsh@root", cmd_id="export", client_id="browser"))
    assert first.export_id == "export" and not first.done
    assert len(base64.b64decode(first.data)) == CHUNK_SIZE and not is_downstream(first)
    filename = runtime.features.exports[first.export_id][2]
    other = await machine._handle(DownloadDsh(sid="dsh@root", cmd_id="other", export_id=first.export_id, offset=CHUNK_SIZE, client_id="foreign"))
    assert other.error and not other.data
    last = await machine._handle(DownloadDsh(sid="dsh@root", cmd_id="last", export_id=first.export_id, offset=CHUNK_SIZE, client_id="browser"))
    assert last.done and first.total == last.total
    assert base64.b64decode(first.data + "") + base64.b64decode(last.data) == payload
    await machine._handle(DownloadDsh(sid="dsh@root", cmd_id="cancel", export_id=first.export_id, cancel=True, client_id="browser"))
    assert not Path(filename).exists()
    await runtime.close()


@pytest.mark.asyncio
async def test_export_cancel_before_first_chunk_removes_spool():
    machine, _, runtime, client = setup_runtime()
    entered = asyncio.Event()
    filenames = []
    async def export(sid, filename, limit):
        filenames.append(filename)
        entered.set()
        await asyncio.Event().wait()
    client.export_session = export
    task = asyncio.create_task(machine._handle(DownloadDsh(sid="dsh@root", cmd_id="slow-export", client_id="browser")))
    await entered.wait()
    await machine._handle(DownloadDsh(sid="dsh@root", cmd_id="cancel", export_id="slow-export", cancel=True, client_id="browser"))
    response = await task
    assert response.error == "导出已取消"
    assert not Path(filenames[0]).exists() and not runtime.features.exports
    await runtime.close()


@pytest.mark.asyncio
async def test_plan_review_returns_native_option_and_ignores_stale_projection():
    machine, _, runtime, client = setup_runtime()
    ctx = await runtime.ctx("dsh@root")
    runtime.event_client = "native-listener"
    captured = {}
    async def ask(target, question, options, **fields):
        assert target is ctx
        captured.update(question=question, options=options, **fields)
        return "Approve"
    machine._on_ask_locked = ask
    await runtime._question({"eventId": "review", "agentId": "root", "event": "user-questions/request",
        "request": {"questions": [{"id": "plan-review", "question": "Review plan", "detail": "# Build\n\n1. Compile",
            "options": [{"label": "Approve"}, {"label": "Keep planning"}],
            "intent": {"kind": "plan-review", "approve": "Approve"}}]}}, runtime.event_client)
    assert captured["header"] == "计划确认" and "# Build" in captured["question"]
    assert client.calls[-1][1]["outcome"]["value"]["answers"] == [{"id": "plan-review", "selected": ["Approve"]}]
    await runtime.projections(ctx, {"plan": {"active": False, "pending": True}}, seq=20)
    assert ctx.sdk.state.plan_pending and not ctx.sdk.state.plan_active
    await runtime.projections(ctx, {"plan": {"active": True, "pending": False}}, seq=21)
    await runtime.projections(ctx, {"plan": {"active": False, "pending": True}}, seq=19)
    assert ctx.sdk.state.plan_active and not ctx.sdk.state.plan_pending
    await runtime.close()


@pytest.mark.asyncio
async def test_background_jobs_are_scoped_and_clear_when_completed():
    _, _, runtime, _ = setup_runtime()
    parent = await runtime.ctx("dsh@root")
    other = await runtime.ctx("dsh@other")
    await runtime.update_jobs("root", [{"id": "compile", "label": "Build", "status": "running"}])
    assert parent.sdk.state.jobs[0].id == "compile" and not other.sdk.state.jobs
    await runtime.update_jobs("root", [])
    assert not parent.sdk.state.jobs
    await runtime.close()


@pytest.mark.asyncio
async def test_cold_conversation_keeps_append_messages_and_filters_surface_operations():
    machine, _, runtime, client = setup_runtime()
    async def snapshot(sid, **options):
        assert options["before_seq"] == 11 and options["query"] == "needle"
        return {"hasMore": True, "records": [
            {"event": {"seq": 4, "type": "user/message", "surfaceOp": "append", "data": {"source": {"kind": "user"}, "content": [{"type": "text", "text": "needle"}]}}},
            {"event": {"seq": 5, "type": "assistant/message", "surfaceOp": "append", "data": {"content": [{"type": "text", "text": "answer"}]}}},
            {"event": {"seq": 6, "type": "assistant/message", "surfaceOp": {"kind": "replace"}, "data": {"content": [{"type": "text", "text": "compacted"}]}}},
        ]}
    client.history_snapshot = snapshot
    result = await machine._handle(ReadDsh(sid="dsh@root", kind="conversation", query="needle", before_seq=11, cmd_id="history", client_id="browser"))
    assert [item.detail for item in result.items] == ["needle", "answer"]
    assert result.has_more and result.next_seq == 4 and not machine.sessions
    await runtime.close()


@pytest.mark.asyncio
async def test_archive_rejects_query_lock_even_before_runtime_turn_starts():
    machine, _, runtime, client = setup_runtime()
    ctx = await runtime.ctx("dsh@root")
    async with ctx.query_lock:
        result = await machine._handle(ArchiveSession(session_id=ctx.key, engine="dsh", archived=True, cmd_id="archive", client_id="browser"))
    assert result.code == "dsh_archive_busy"
    assert not any(call[0] == "workspace/archiveSession" for call in client.calls)
    await runtime.close()
