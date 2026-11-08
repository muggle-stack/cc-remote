import json
from uuid import uuid4

import pytest

from cc_remote.protocol import GetAgentDetail, ReadDsh, SetDshControl, is_downstream
from tests.test_dsh_runtime import setup_runtime


def command(action, **values):
    return SetDshControl(sid="dsh@root", client_id="browser", cmd_id=uuid4().hex, kind="manage", value=json.dumps({"action": action, **values}))


@pytest.mark.asyncio
async def test_settings_exclude_credentials_and_reject_stale_revision():
    machine, _, runtime, client = setup_runtime()
    calls = []
    async def rpc(endpoint, args=None):
        calls.append((endpoint, args))
        if endpoint == "settings/describe":
            return {"writable": True, "namespaces": [
                {"ns": "llm-deepseek", "revision": 1, "value": {"key": "private"}},
                {"ns": "subagent", "revision": 4, "value": {"maxConcurrency": 6, "secret": "private", "proxy": "private", "nested": {"key": "private"}}, "secrets": [{"path": ["proxy"], "set": True}]},
            ]}
        return {"ns": args["ns"], "value": args["patch"], "applies": "live", "revision": 5}
    client.rpc = rpc
    view = await machine._handle(ReadDsh(sid="dsh@root", kind="settings", client_id="browser", cmd_id="read"))
    assert len(view.items) == 1
    assert view.items[0].data == {"revision": 4, "values": {"maxConcurrency": 6}}
    assert not is_downstream(view) and not machine.sessions
    for revision, patch in [(3, {"maxConcurrency": 3}), (4, {"proxy": "bad"})]:
        result = await machine._handle(command("settings", ns="subagent", revision=revision, patch=patch))
        assert result.status == "error"
    assert not any(e == "settings/update" for e, _ in calls)
    result = await machine._handle(command("settings", ns="subagent", revision=4, patch={"maxConcurrency": 3}))
    assert result.status == "success"
    assert calls[-1] == ("settings/update", {"ns": "subagent", "expectedRevision": 4, "patch": {"maxConcurrency": 3}})
    assert not machine.sessions
    await runtime.close()


@pytest.mark.asyncio
async def test_schedule_edit_preserves_original_session_and_observed_record():
    machine, _, runtime, client = setup_runtime()
    record = {"id": "task", "kind": "every", "title": "Review", "prompt": "Check", "everySeconds": 300, "scheduledAt": "2026-10-22T00:00:00Z"}
    calls = []
    async def rpc(endpoint, args=None):
        calls.append((endpoint, args))
        if endpoint == "schedule/catalog":
            return [{**record, "target": {"kind": "session", "sessionId": "other"}, "status": "active", "updatedAt": "rev1"}]
        if endpoint == "schedule/history":
            return {"records": [{"messageId": "delivery", "deliveredAt": "2026-10-22T00:00:00Z"}]}
        return {**record, "updatedAt": "rev2"}
    client.rpc = rpc
    result = await machine._handle(command("schedule_update", id="task", expectedUpdatedAt="rev1", patch={"title": "Review code", "prompt": "New", "timing": {"kind": "every", "every_seconds": 600}}))
    assert result.status == "success"
    assert calls[-1] == ("schedule/edit", {"request": {"id": "task", "action": "update", "expectedUpdatedAt": "rev1", "patch": {"title": "Review code", "prompt": "New", "timing": {"kind": "every", "every_seconds": 600}}}})
    page = await machine._handle(ReadDsh(sid="dsh@root", target_sid="dsh@other", kind="schedule_history", query="task", client_id="browser", cmd_id="history"))
    assert page.items[0].detail == "delivery"
    assert not machine.sessions
    await runtime.close()


@pytest.mark.asyncio
async def test_child_detail_is_private_and_does_not_follow_or_reside():
    machine, _, runtime, client = setup_runtime()
    client.summaries = {
        "root": {"sessionId": "root"},
        "child": {"sessionId": "child", "parentSessionId": "root", "origin": "subagent", "running": False},
        "grandchild": {"sessionId": "grandchild", "parentSessionId": "child", "origin": "subagent", "running": False},
    }
    async def catalog(sid):
        return {"entries": [{"id": "grandchild", "label": "Regression"}]}
    client.list_subagents = catalog
    result = await machine._handle(GetAgentDetail(session_id="dsh@root", run_id="dsh@grandchild", request_id="read-child", client_id="browser"))
    assert result.authoritative and not result.error and result.parent_run_id == "dsh@child"
    assert result.title == "Regression" and result.to == "browser"
    assert not machine.sessions and not is_downstream(result)
    result = await machine._handle(GetAgentDetail(session_id="dsh@root", run_id="dsh@foreign", request_id="bad-child", client_id="browser"))
    assert not result.authoritative and result.error
    assert not any(call[0] == "snapshot" and call[1] == "dsh@foreign" for call in client.calls)
    await runtime.close()


@pytest.mark.asyncio
async def test_paged_workflow_keeps_partial_terminal_identity_for_older_page_merge():
    machine, _, runtime, client = setup_runtime()
    async def snapshot(sid, **kwargs):
        return {"hasMore": True, "records": [
            {"event": {"seq": 100, "type": "tool-workflow/agent-end", "data": {"runId": "run", "seq": 2, "outcome": "failed"}}},
            {"event": {"seq": 101, "type": "tool-workflow/run-end", "data": {"runId": "run", "stopReason": "completed"}}},
        ]}
    client.history_snapshot = snapshot
    page = await machine._handle(ReadDsh(sid="dsh@root", kind="workflows", client_id="browser", cmd_id="read"))
    assert page.next_seq == 100 and page.has_more
    assert page.items[0].state == "completed"
    assert page.items[0].data == {"name_known": False, "members": [{"seq": 2, "state": "failed"}]}
    assert not machine.sessions
    await runtime.close()


@pytest.mark.asyncio
async def test_native_bundles_include_optional_shipped_controls_and_missing_installs():
    machine, _, runtime, client = setup_runtime()
    async def rpc(endpoint, args=None):
        return [
            {"name": "core", "enabled": True, "installed": False, "availability": "installation", "readOnlyReason": "management-required"},
            {"name": "optional", "enabled": False, "installed": False, "optional": True, "availability": "installation", "meta": {"title": {"zh-CN": "语音输入"}}},
            {"name": "extra", "enabled": False, "availability": "missing", "installTarget": {"spec": "extra@1.0"}},
        ]
    client.rpc = rpc
    page = await machine._handle(ReadDsh(sid="dsh@root", kind="bundles", client_id="browser", cmd_id="read"))
    core, optional, missing = page.items
    assert not core.controllable and core.data["readOnlyReason"] == "management-required"
    assert optional.controllable and optional.title == "语音输入" and optional.state == "disabled"
    assert not missing.controllable and missing.state == "not-installed" and missing.data["installTarget"]["spec"] == "extra@1.0"
    assert not machine.sessions
    await runtime.close()


@pytest.mark.asyncio
async def test_market_search_uses_only_native_configured_source_and_paginates():
    machine, _, runtime, client = setup_runtime()
    calls = []
    async def rpc(endpoint, args=None):
        calls.append((endpoint, args))
        if endpoint == "pluginMarket/listIndexes":
            return [{"url": "https://example.org/approved-index"}]
        if endpoint == "pluginManager/listBundles":
            return []
        return {"entries": [{"name": "example", "title": {"zh-CN": "示例"}, "defaultVersion": "2.0", "versions": [
            {"version": "2.0", "install": {"spec": "example@2.0"}}], "installation": {"status": "update-available", "installedVersion": "1.0"}}], "nextOffset": 60}
    client.rpc = rpc
    page = await machine._handle(ReadDsh(sid="dsh@root", kind="bundles", query='{"market":true,"text":"test","url":"https://untrusted.invalid"}', before_seq=30, client_id="browser", cmd_id="read"))
    assert page.has_more and page.next_seq == 60
    assert page.items[0].state == "update-available" and page.items[0].data["installTarget"] is None
    assert next(args for endpoint, args in calls if endpoint == "pluginMarket/search") == {"url": "https://example.org/approved-index", "query": {"text": "test", "sort": {"kind": "relevance"}, "offset": 30, "limit": 30}}
    await runtime.close()


def test_settings_preserve_native_numeric_limits_and_unset_default_preset():
    from cc_remote.wrapper.dsh_management import visible_settings
    result = visible_settings({"writable": True, "namespaces": [
        {"ns": "subagent", "revision": 0, "applies": "live", "value": {"maxDepth": 1}, "schema": {"uid": 2, "refs": {
            "2": {"dict": {"maxDepth": 1}}, "1": {"type": "number", "meta": {"min": 0, "step": 1}}}}},
        {"ns": "agent-preset-registry", "revision": 0, "applies": "live", "value": {}, "schema": {"uid": 2, "refs": {
            "2": {"dict": {"selectedDefault": 1}}, "1": {"type": "string"}}}},
    ]})
    assert result[0].data["constraints"] == {"maxDepth": {"min": 0, "step": 1}}
    assert result[1].data["values"] == {"selectedDefault": None}


@pytest.mark.asyncio
async def test_settings_receipt_must_confirm_the_effective_values():
    machine, _, runtime, client = setup_runtime()
    async def rpc(endpoint, args=None):
        if endpoint == "settings/describe":
            return {"writable": True, "namespaces": [{"ns": "subagent", "revision": 1, "value": {"maxDepth": 1}}]}
        return {"ns": "subagent", "value": {"maxDepth": 1}, "revision": 2}
    client.rpc = rpc
    result = await machine._handle(command("settings", ns="subagent", revision=1, patch={"maxDepth": 2}))
    assert result.status != "success" and "实际值" in result.text
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["[]", "null", "not-json", '{"market":false}', '{"market":true,"text":42}'])
async def test_invalid_market_request_does_not_call_native_sources(query):
    machine, _, runtime, client = setup_runtime()
    calls = []
    async def rpc(endpoint, args=None):
        calls.append(endpoint)
        raise AssertionError("invalid search must not read native sources")
    client.rpc = rpc
    page = await machine._handle(ReadDsh(sid="dsh@root", kind="bundles", query=query, client_id="browser", cmd_id="read"))
    assert page.error and not calls
    await runtime.close()
