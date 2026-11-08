"""Statistics, cleanup and plugin product flows at the private command boundary."""
import asyncio
from uuid import uuid4

import pytest

from cc_remote.protocol import ReadDsh
from cc_remote.wrapper.dsh_client import DshError
from cc_remote.wrapper.dsh_plugins import source_id, sources_revision
from cc_remote.wrapper.dsh_stats import project
from tests.test_dsh_022 import command
from tests.test_dsh_runtime import setup_runtime


def event(kind, time, **data):
    return {"type": kind, "time": time, "data": data}


def test_statistics_pair_parallel_tools_ptc_and_weight_cache_by_tokens():
    events = [event("turn/start", 100, turn=1), event("step/start", 100, turn=1, step=1),
        event("assistant/message", 300, turn=1, step=1, usage={"inputTokens": 20, "cacheReadTokens": 80, "outputTokens": 10},
              stream=[{"type": "text-chunks", "time0": 150, "texts": ["", "hello"], "dt": [10]}]),
        event("tool/call", 300, turn=1, step=1, callId="a", name="run_code"),
        event("tool/call", 310, turn=1, step=1, callId="b", name="read"),
        event("tool/ptc-dispatch-start", 330, rootCallId="a", parentCallId="a", subCallId="inner", name="bash"),
        event("tool/ptc-dispatch", 400, rootCallId="a", parentCallId="a", subCallId="inner", name="bash"),
        event("tool/result", 450, turn=1, step=1, message={"toolCallId": "b"}),
        event("tool/result", 500, turn=1, step=1, message={"toolCallId": "a"}),
        event("step/end", 500, turn=1, step=1), event("step/start", 500, turn=1, step=2),
        event("assistant/message", 600, turn=1, step=2, usage={"inputTokens": 900, "cacheReadTokens": 0, "outputTokens": 20}),
        event("step/end", 600, turn=1, step=2), event("turn/end", 600, turn=1, reason={"kind": "success"})]
    row = project(events)[0].data
    assert row["wall"] == 500 and row["model"] == 300
    assert row["tool"] == 200  # not outer + overlapping read + inner
    assert row["input"] == 1000 and row["output"] == 30 and row["cacheHit"] == .08
    assert row["steps"][0]["ttft"] == 60
    assert next(tool for tool in row["tools"] if tool["name"] == "bash")["duration"] == 70


def test_statistics_missing_boundaries_never_invent_zero_or_elapsed_wall():
    row = project([event("assistant/message", 1000, turn=2, step=1),
                   event("tool/result", 1100, turn=2, step=1, message={"toolCallId": "unknown"})])[0].data
    assert row["wall"] is row["model"] is row["input"] is row["cacheHit"] is None
    assert row["partial"] and not row["tools"]


@pytest.mark.asyncio
async def test_cleanup_reads_full_catalog_rechecks_revision_and_never_replays_unknown_write():
    machine, _, runtime, client = setup_runtime()
    rows = [{"id": str(i), "status": "ended", "updatedAt": "1", "target": {"kind": "session", "sessionId": "root"}} for i in range(140)]
    rows.append({"id": "active", "status": "active", "target": {"kind": "session", "sessionId": "root"}})
    calls = []
    async def rpc(endpoint, args=None):
        calls.append(endpoint)
        if endpoint == "schedule/catalog":
            return rows
        assert endpoint == "schedule/edit"
        assert args == {"request": {"id": "0", "expectedUpdatedAt": "2", "action": "delete"}}
        raise DshError("lost", "未知", outcome_unknown=True)
    client.rpc = rpc
    plan = await machine._handle(ReadDsh(sid="dsh@root", kind="schedules", query="cleanup", cmd_id="plan"))
    assert plan.items[0].data["count"] == 140
    rows[0]["updatedAt"] = "2"
    refused = await machine._handle(command("schedule_cleanup", revision=plan.items[0].data["revision"]))
    assert refused.status == "error" and "schedule/edit" not in calls
    plan = await machine._handle(ReadDsh(sid="dsh@root", kind="schedules", query="cleanup", cmd_id="plan2"))
    cmd = command("schedule_cleanup", revision=plan.items[0].data["revision"])
    assert (await machine._handle(cmd)).status == "unknown"
    assert (await machine._handle(cmd)).status == "unknown"
    assert calls.count("schedule/edit") == 1 and "schedule/deleteInactive" not in calls and not machine.sessions
    await runtime.close()


@pytest.mark.asyncio
async def test_cleanup_cannot_remove_private_ended_tasks_through_global_api():
    machine, _, runtime, client = setup_runtime()
    runtime.sidechats.rows["dsh@hidden"] = {"owner": "another", "parent": "dsh@root", "created": 1, "cut": 1}
    async def rpc(endpoint, args=None):
        assert endpoint == "schedule/catalog"
        return [{"id": "task", "status": "ended", "target": {"kind": "session", "sessionId": "hidden"}}]
    client.rpc = rpc
    plan = await machine._handle(ReadDsh(sid="dsh@root", kind="schedules", query="cleanup", cmd_id="plan"))
    assert plan.items[0].data["count"] == 0
    assert (await machine._handle(command("schedule_cleanup", revision=plan.items[0].data["revision"]))).status == "success"
    await runtime.close()


@pytest.mark.asyncio
async def test_cleanup_waits_for_an_ended_tasks_inflight_delivery():
    machine, _, runtime, client = setup_runtime()
    async def rpc(endpoint, args=None):
        assert endpoint == "schedule/catalog"
        return [{"id": "task", "status": "ended", "executionActive": True, "target": {"kind": "session", "sessionId": "root"}}]
    client.rpc = rpc
    plan = await machine._handle(ReadDsh(sid="dsh@root", kind="schedules", query="cleanup", cmd_id="plan"))
    result = await machine._handle(command("schedule_cleanup", revision=plan.items[0].data["revision"]))
    assert result.status == "error" and "仍在投递" in result.text
    await runtime.close()


@pytest.mark.asyncio
async def test_cleanup_only_deletes_confirmed_ids_when_more_visible_and_private_tasks_end():
    machine, _, runtime, client = setup_runtime()
    runtime.sidechats.rows["dsh@hidden"] = {"owner": "another", "parent": "dsh@root", "created": 1, "cut": 1}
    rows = [{"id": str(i), "status": "ended", "updatedAt": "1", "target": {"kind": "session", "sessionId": "root"}} for i in range(140)]
    requests = []
    async def rpc(endpoint, args=None):
        if endpoint == "schedule/catalog":
            return rows
        assert endpoint == "schedule/edit"
        request = args["request"]
        requests.append(request)
        if len(requests) == 1:
            # Both ended after the last catalog read, before its first mutation.
            rows.extend({"id": sid, "status": "ended", "updatedAt": "2",
                         "target": {"kind": "session", "sessionId": sid}} for sid in ("new", "hidden"))
        row = next(row for row in rows if row["id"] == request["id"])
        assert request == {"id": row["id"], "expectedUpdatedAt": row["updatedAt"], "action": "delete"}
        rows.remove(row)
        return {"id": row["id"], "deleted": True}
    client.rpc = rpc
    plan = await machine._handle(ReadDsh(sid="dsh@root", kind="schedules", query="cleanup", cmd_id="plan"))
    assert plan.items[0].data["count"] == 140
    result = await machine._handle(command("schedule_cleanup", revision=plan.items[0].data["revision"]))
    assert result.status == "success" and "140" in result.text
    assert [r["id"] for r in rows] == ["new", "hidden"]
    assert len(requests) == 140 and not machine.sessions
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["conflict", "lost", "malformed"])
async def test_cleanup_stops_on_partial_failure_and_replays_only_the_saved_result(failure):
    machine, _, runtime, client = setup_runtime()
    rows = [{"id": str(i), "status": "ended", "updatedAt": "1", "target": {"kind": "session", "sessionId": "root"}} for i in range(3)]
    requests = []
    async def rpc(endpoint, args=None):
        if endpoint == "schedule/catalog":
            return rows
        assert endpoint == "schedule/edit"
        request = args["request"]
        requests.append(request)
        if len(requests) == 1:
            # Freeze all revisions before writes: a later row changed in flight.
            rows[1]["updatedAt"] = "2"
            rows.pop(0)
            return {"id": "0", "deleted": True}
        assert request == {"id": "1", "expectedUpdatedAt": "1", "action": "delete"}
        if failure == "lost":
            raise DshError("lost", "结果未知。", outcome_unknown=True)
        return {"id": "1", "code": "schedule_conflict"} if failure == "conflict" else {"deleted": True}
    client.rpc = rpc
    plan = await machine._handle(ReadDsh(sid="dsh@root", kind="schedules", query="cleanup", cmd_id="plan"))
    cmd = command("schedule_cleanup", revision=plan.items[0].data["revision"])
    result = await machine._handle(cmd)
    assert result.status == ("error" if failure == "conflict" else "unknown")
    assert "已确认清理 1 个" in result.text and "后续任务未执行" in result.text
    from cc_remote.wrapper.dsh_mutations import DshMutationJournal
    runtime.mutations = DshMutationJournal(machine.cfg.state_dir)
    replay = await machine._handle(cmd)
    assert (replay.status, replay.text) == (result.status, result.text)
    assert len(requests) == 2 and [r["id"] for r in rows] == ["1", "2"]
    await runtime.close()


@pytest.mark.asyncio
async def test_plugin_sources_preserve_private_config_and_require_observed_revision():
    machine, _, runtime, client = setup_runtime()
    sources = [{"url": "https://index.test/private?token=private"}]
    writes = []
    async def rpc(endpoint, args=None):
        if endpoint == "pluginMarket/listIndexes":
            return sources
        assert endpoint == "pluginMarket/setIndexes"
        writes.append(args)
        sources[:] = [{"url": url} for url in args["urls"]]
    client.rpc = rpc
    page = await machine._handle(ReadDsh(sid="dsh@root", kind="bundles", query='{"sources":true}', cmd_id="sources"))
    assert "private" not in page.model_dump_json() and page.items[0].data["url"] is None
    revision = page.items[0].data["revision"]
    for url in ("https://user:password@host/index", "https://host/index?token=secret", "file:///tmp/index"):
        assert (await machine._handle(command("market_source_add", revision=revision, url=url))).status == "error"
    assert not writes
    assert (await machine._handle(command("market_source_add", revision=revision, url="https://public.test/index.json"))).status == "success"
    assert writes[0]["urls"] == ["https://index.test/private?token=private", "https://public.test/index.json"]
    assert (await machine._handle(command("market_source_remove", revision=revision, source=page.items[0].id))).status == "error"
    assert len(writes) == 1
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("compatible", [True, False])
async def test_market_install_uses_only_authoritative_target_and_compatibility(compatible):
    machine, _, runtime, client = setup_runtime()
    source = {"url": "https://market.test/index.json"}
    calls = []
    async def rpc(endpoint, args=None):
        calls.append((endpoint, args))
        if endpoint == "pluginMarket/listIndexes":
            return [source]
        if endpoint == "pluginMarket/search":
            return {"entries": [{"name": "example", "installation": {"status": "not-installed"}, "versions": [{
                "version": "1.0.0", "compatibility": {"compatible": compatible},
                "install": {"spec": "example@1.0.0", "registry": "https://native.registry/"}}]}]}
        if endpoint == "pluginManager/inspect":
            return {"status": "accepted"}
        assert endpoint == "pluginManager/installBundle"
        assert args["spec"] == "example@1.0.0" and args["options"]["registry"] == "https://native.registry/"
        return {"application": "applied"}
    client.rpc = rpc
    result = await machine._handle(command("market_install", source=source_id(source), name="example", version="1.0.0", spec="evil", registry="https://evil.test"))
    assert result.status == ("success" if compatible else "error")
    assert any(name == "pluginManager/installBundle" for name, _ in calls) == compatible
    await runtime.close()


@pytest.mark.asyncio
async def test_detail_reads_are_lazy_bounded_and_do_not_activate_native_session():
    machine, _, runtime, client = setup_runtime()
    async def rpc(endpoint, args=None):
        assert endpoint == "pluginManager/readBundleDetail"
        assert args == {"name": "example", "language": "zh-CN"}
        return {"status": "found", "markdown": "# Detail\n" + "x" * 70000, "document": "private-native-reference"}
    client.rpc = rpc
    page = await machine._handle(ReadDsh(sid="dsh@root", kind="bundles", query='{"detail":true,"name":"example"}', cmd_id=uuid4().hex))
    assert len(page.items[0].detail) == 65536 and page.items[0].data["truncated"]
    assert "private-native-reference" not in page.model_dump_json() and not machine.sessions
    await runtime.close()


@pytest.mark.asyncio
async def test_empty_market_exposes_source_revision_without_fake_default():
    machine, _, runtime, client = setup_runtime()
    async def rpc(endpoint, args=None):
        assert endpoint == "pluginMarket/listIndexes"
        return []
    client.rpc = rpc
    page = await machine._handle(ReadDsh(sid="dsh@root", kind="bundles", query='{"sources":true}', cmd_id="empty"))
    assert page.items[0].state == "empty" and page.items[0].data["revision"] == sources_revision([])
    await runtime.close()


@pytest.mark.asyncio
async def test_concurrent_source_edits_do_not_overwrite_another_browser():
    machine, _, runtime, client = setup_runtime()
    rows, writes = [], []
    async def rpc(endpoint, args=None):
        if endpoint == "pluginMarket/listIndexes":
            await asyncio.sleep(0)
            return list(rows)
        assert endpoint == "pluginMarket/setIndexes"
        writes.append(args["urls"])
        await asyncio.sleep(0)
        rows[:] = [{"url": url} for url in args["urls"]]
    client.rpc = rpc
    replies = await asyncio.gather(*(machine._handle(command("market_source_add",
        revision=sources_revision([]), url=f"https://{name}.test/index.json")) for name in ("first", "second")))
    assert sorted(result.status for result in replies) == ["error", "success"]
    assert len(writes) == len(rows) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_recommendations_bind_exact_native_owner_and_keep_dismissal_on_reload():
    machine, _, runtime, client = setup_runtime()
    proposals = {key: {"action": "enable_official_plugin", "details": {"name": "official"}}
                 for key in ("first", "second", "missing")}
    proposals["page"] = {"action": "create_personalized_page"}
    decisions, writes = {}, []
    records = []
    def add(kind, **data):
        records.append({"event": {"seq": len(records), "type": kind, "time": len(records) * 1000, "data": data}})
    add("turn/start", turn=1)
    add("user/message", source={"kind": "user"}, content=[{"type": "text", "text": "one"}])
    add("tool/call", callId="first", name="propose_action", turn=1, step=1)
    add("user/message", source={"kind": "user"}, content=[{"type": "text", "text": "steer"}])
    add("tool/call", callId="second", name="propose_action", turn=1, step=2)
    async def rpc(endpoint, args=None):
        if endpoint == "session/projections":
            return {"kind": "sequenced", "values": {"actionCards": {"proposals": proposals, "decisions": decisions}}}
        if endpoint == "pluginManager/listBundles":
            return [{"name": "official", "official": True, "optional": True, "availability": "present"}]
        assert endpoint == "actionCards/act"
        writes.append(args)
        decision = {"callId": args["callId"], "action": "enable_official_plugin", "phase": "dismissed"}
        decisions[args["callId"]] = decision
        return {"status": "settled", "decision": decision}
    async def snapshot(*args, **kwargs):
        return {"records": records}
    client.rpc, client.history_snapshot = rpc, snapshot
    client.summaries["root"] = {"sessionId": "root", "agentAvailable": True}
    async def read():
        return await machine._handle(ReadDsh(sid="dsh@root", kind="plugins", query="recommendations", cmd_id=uuid4().hex))
    rows = (await read()).items
    assert [(row.id, row.data["owner"]) for row in rows] == [("first", "dsh-seq-1"), ("second", "dsh-seq-3")]
    assert all(row.controllable for row in rows) and not machine.sessions
    assert (await machine._handle(command("recommendation", call="page", verb="enable"))).status == "error"
    assert (await machine._handle(command("recommendation", call="first", verb="dismiss"))).status == "success"
    assert [row.id for row in (await read()).items] == ["second"]
    client.summaries["root"]["agentAvailable"] = False
    assert not (await read()).items[0].controllable
    assert writes == [{"agentId": "root", "callId": "first", "verb": "dismiss"}]
    await runtime.close()


@pytest.mark.asyncio
async def test_unknown_recommendation_receipt_is_not_replayed():
    machine, _, runtime, client = setup_runtime()
    writes = []
    async def rpc(endpoint, args=None):
        if endpoint == "session/projections":
            return {"kind": "sequenced", "values": {"actionCards": {
                "proposals": {"card": {"action": "enable_official_plugin"}}, "decisions": {}}}}
        assert endpoint == "actionCards/act"
        writes.append(args)
        return {"status": "unconfirmed"}
    client.rpc = rpc
    cmd = command("recommendation", call="card", verb="enable")
    assert (await machine._handle(cmd)).status == "unknown"
    assert (await machine._handle(cmd)).status == "unknown"
    assert len(writes) == 1
    await runtime.close()
