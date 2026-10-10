"""Upgrade acceptance must exercise the thread's MCP, not a fresh global pool."""
from contextlib import asynccontextmanager
import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest

from cc_remote import async_tasks, task_refresh


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    home = tmp_path / "account"
    home.mkdir()
    release = tmp_path / "release-new"
    (release / "cc_remote").mkdir(parents=True)
    for name in ("async_tasks.py", "task_store.py", "task_rpc.py"):
        (release / "cc_remote" / name).touch()
    entry = {"command": "/usr/local/bin/cc-remote",
             "args": ["tasks", "mcp", "--codex-home", str(home)],
             "env": {"EXISTING_OPTION": "preserved"}}
    config = {"mcp_servers": {"cc_remote_tasks": entry}, "model": "unchanged-model"}
    (home / "config.toml").write_text("# parsed by the fixture\n")
    monkeypatch.setattr(task_refresh.tomllib, "loads", lambda _: copy.deepcopy(config))
    calls = []
    control = {"stale": True, "reject_write": False, "bad_call": False, "override": False}

    class Native:
        async def thread(self, sid):
            calls.append(("thread/read", {"threadId": sid}))
            return {"id": sid, "cwd": str(home / "project")}

        async def rpc(self, method, params, **kwargs):
            calls.append((method, copy.deepcopy(params)))
            if method == "config/read":
                effective = copy.deepcopy(config)
                if control["override"]:
                    effective["mcp_servers"]["cc_remote_tasks"]["cwd"] = "/project/override"
                return {"config": effective, "layers": [{
                    "name": {"type": "user", "file": str(home / "config.toml")},
                    "version": "native-version",
                }]}
            if method == "config/batchWrite":
                if control["reject_write"]:
                    raise task_refresh.task_rpc.Rejected("version conflict")
                assert params["expectedVersion"] == "native-version"
                assert params["reloadUserConfig"] is False
                assert params["edits"] == [{"keyPath": "mcp_servers.cc_remote_tasks.cwd",
                                          "value": str(release), "mergeStrategy": "replace"}]
                entry["cwd"] = str(release)
                control["stale"] = False
                return {"status": "ok"}
            if method == "config/mcpServer/reload":
                assert params is None
                # Reusing unchanged config is the actual production failure.
                return {}
            if method == "mcpServerStatus/list":
                assert params["threadId"]
                assert params["serverName"] == "cc_remote_tasks"
                # Both old and new implementations expose the same catalog.
                return {"data": [{"name": "cc_remote_tasks",
                                  "tools": dict.fromkeys(task_refresh.TASK_TOOLS, {})}]}
            if method == "mcpServer/tool/call":
                assert params["tool"] == "task_status"
                assert params["arguments"] == {}
                if control["stale"] or control["bad_call"]:
                    return {"isError": True, "content": []}
                return {"isError": False, "structuredContent": {"tasks": []}}
            pytest.fail(f"Unexpected native method: {method}")

    @asynccontextmanager
    async def connect(selected_home):
        assert selected_home == home
        yield Native()

    monkeypatch.setattr(task_refresh.task_rpc, "connect", connect)
    return home, release, config, calls, control


@pytest.mark.asyncio
async def test_upgrade_replaces_stale_thread_pool_and_verifies_real_call(runtime):
    home, release, config, calls, _ = runtime
    before = copy.deepcopy(config)
    result = await task_refresh.refresh(home, release, str(uuid4()))
    assert result["configuration_changed"] and result["thread_verified"]
    assert result["task_count"] == 0
    expected = before
    expected["mcp_servers"]["cc_remote_tasks"]["cwd"] = str(release)
    assert config == expected
    assert [method for method, _ in calls] == [
        "thread/read", "config/read", "config/batchWrite", "config/mcpServer/reload",
        "mcpServerStatus/list", "mcpServer/tool/call",
    ]
    assert calls[1][1]["cwd"] == str(home / "project")
    assert not (home / "cc-remote-async-tasks").exists()


@pytest.mark.asyncio
async def test_same_release_does_not_rewrite_config_but_still_verifies(runtime):
    home, release, config, calls, control = runtime
    config["mcp_servers"]["cc_remote_tasks"]["cwd"] = str(release)
    control["stale"] = False
    result = await task_refresh.refresh(home, release, str(uuid4()))
    assert not result["configuration_changed"] and result["thread_verified"]
    assert not any(method == "config/batchWrite" for method, _ in calls)


@pytest.mark.asyncio
async def test_catalog_success_does_not_hide_failed_thread_call(runtime):
    home, release, _, _, control = runtime
    control["bad_call"] = True
    with pytest.raises(ValueError, match="task_status call failed"):
        await task_refresh.refresh(home, release, str(uuid4()))


@pytest.mark.asyncio
async def test_no_thread_reports_only_requested_not_verified(runtime):
    home, release, _, calls, _ = runtime
    result = await task_refresh.refresh(home, release)
    assert result["refresh_requested"] and not result["thread_verified"]
    assert all(not method.startswith(("thread/", "mcpServerStatus/", "mcpServer/tool/"))
               for method, _ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "disabled", "other_account", "custom", "remote"])
async def test_does_not_enable_or_replace_custom_servers(runtime, change):
    home, release, config, calls, _ = runtime
    entry = config["mcp_servers"]["cc_remote_tasks"]
    if change == "missing":
        config["mcp_servers"].clear()
    elif change == "disabled":
        entry["enabled"] = False
    elif change == "other_account":
        entry["args"][-1] = str(home.parent / "other")
    elif change == "custom":
        entry["command"] = "/custom/server"
    else:
        entry["environment_id"] = "remote"
    with pytest.raises(ValueError):
        await task_refresh.refresh(home, release)
    assert not calls


@pytest.mark.asyncio
async def test_project_override_is_not_promoted_to_user_config(runtime):
    home, release, _, calls, control = runtime
    control["override"] = True
    with pytest.raises(ValueError, match="overridden"):
        await task_refresh.refresh(home, release)
    assert [method for method, _ in calls] == ["config/read"]


@pytest.mark.asyncio
async def test_native_version_conflict_does_not_overwrite_or_retry(runtime):
    home, release, _, calls, control = runtime
    control["reject_write"] = True
    with pytest.raises(task_refresh.task_rpc.Rejected):
        await task_refresh.refresh(home, release)
    assert [method for method, _ in calls] == ["config/read", "config/batchWrite"]


def test_refresh_cli_does_not_create_worker_or_task_store(monkeypatch, tmp_path, capsys):
    async def refresh(home, release, sid):
        assert home == tmp_path and release == Path(async_tasks.__file__).resolve().parent.parent
        assert sid is None
        return {"thread_verified": False}

    monkeypatch.setattr(task_refresh, "refresh", refresh)
    monkeypatch.setattr(async_tasks, "TaskStore", lambda *_: pytest.fail("unexpected task store"))
    assert async_tasks.main(["refresh", "--codex-home", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"thread_verified": False}
