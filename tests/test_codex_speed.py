"""Speed options and mutations follow the account's native model catalog."""
import asyncio

import pytest

from cc_remote.protocol import Fast
from cc_remote.wrapper import codex_handle as handle_module
from cc_remote.wrapper import codex_models as models
from tests.test_codex_controls import _Cfg


CATALOG = models._normalize([
    {"id": "astra", "serviceTiers": [
        {"id": "priority", "name": "Fast", "description": "2x speed"},
        {"id": "ultrafast", "name": "Ultrafast", "description": "Higher usage"},
    ]},
    {"id": "sol", "serviceTiers": [{"id": "priority"}]},
    {"id": "standard-only", "serviceTiers": []},
    {"id": "unknown"},
])


def test_speed_catalog_preserves_entitlements_and_missing_metadata():
    assert [t["id"] for t in CATALOG[0]["service_tiers"]] == ["priority", "ultrafast"]
    assert CATALOG[2]["service_tiers"] == []
    assert CATALOG[3]["service_tiers"] is None
    assert models.resolve_service_tier("unknown", "default", []) is None
    assert models.resolve_service_tier("astra", "fast", CATALOG) == "priority"
    assert models.resolve_service_tier("astra", "ultrafast", CATALOG) == "ultrafast"
    for model in ["sol", "standard-only", "unknown", "missing"]:
        with pytest.raises(ValueError):
            models.resolve_service_tier(model, "ultrafast", CATALOG)


def test_speed_catalog_is_bounded_and_deduplicates_native_aliases():
    result = models._normalize([{"id": "m", "serviceTiers": [
        {"id": "default"}, {"id": "toggle"}, {"id": "invalid id"},
        {"id": "fast", "name": "a" * 1000}, {"id": "priority"},
        *[{"id": f"tier-{i}"} for i in range(30)],
    ]}])[0]["service_tiers"]
    assert len(result) == 16
    assert result[0]["id"] == "priority"
    assert len(result[0]["name"]) == 128


def test_speed_updates_validate_profile_model_and_switch_atomically(monkeypatch, tmp_path):
    async def run():
        handle = handle_module.CodexHandle(_Cfg(), codex_home=str(tmp_path))
        handle.thread_id, handle.model = "thread", "astra"
        homes, calls = [], []

        async def catalog(**kwargs):
            homes.append(kwargs["codex_home"])
            return CATALOG

        async def request(method, params):
            calls.append((method, params))
            return {}

        monkeypatch.setattr(handle_module, "codex_catalog", catalog)
        handle._request = request
        await handle.set_service_tier("ultrafast")
        assert handle.service_tier == "ultrafast"
        await handle.set_model("sol")
        assert handle.model == "sol" and handle.service_tier is None
        assert calls[-1] == ("thread/settings/update", {
            "threadId": "thread", "model": "sol", "serviceTier": None,
        })
        before = len(calls)
        with pytest.raises(ValueError, match="不支持"):
            await handle.set_service_tier("ultrafast")
        assert len(calls) == before
        await handle.set_service_tier("fast")
        assert calls[-1][1]["serviceTier"] == "priority"
        await handle.set_service_tier("default")
        assert calls[-1][1]["serviceTier"] is None
        assert set(homes) == {str(tmp_path)}

    asyncio.run(run())


def test_native_model_change_during_speed_lookup_cannot_set_another_model(monkeypatch, tmp_path):
    async def run():
        handle = handle_module.CodexHandle(_Cfg(), codex_home=str(tmp_path))
        handle.thread_id, handle.model = "thread", "astra"

        async def catalog(**_kwargs):
            handle.model = "sol"  # Another shared CLI changed the native model.
            return CATALOG

        monkeypatch.setattr(handle_module, "codex_catalog", catalog)
        with pytest.raises(ValueError, match="已变化"):
            await handle.set_service_tier("ultrafast")

    asyncio.run(run())


def test_native_speed_notifications_preserve_cycles_and_ignore_foreign_threads(tmp_path):
    async def run():
        events = []

        async def emit(event):
            events.append(event)

        handle = handle_module.CodexHandle(_Cfg(), codex_home=str(tmp_path),
                                          runtime_event_callback=emit)
        handle.thread_id = "thread"

        async def update(tier, thread="thread"):
            await handle._dispatch({"method": "thread/settings/updated", "params": {
                "threadId": thread, "threadSettings": {"serviceTier": tier},
            }})

        await update("ultrafast")
        await update(None)
        await update("ultrafast")
        assert events == []
        await handle.activate_runtime_events()
        assert [e.tier for e in events] == ["ultrafast"]
        await update(None)
        await update("ultrafast")
        await update("priority", "foreign")
        assert all(isinstance(e, Fast) for e in events)
        assert [e.tier for e in events] == ["ultrafast", "default", "ultrafast"]

    asyncio.run(run())


@pytest.mark.parametrize("repeat_cursor", [False, True])
def test_catalog_reads_later_pages_and_rejects_cursor_loops(monkeypatch, repeat_cursor):
    import json
    from types import SimpleNamespace

    async def run():
        replies = asyncio.StreamReader()
        requests = []
        closed = []

        def write(raw):
            request = json.loads(raw)
            if "id" not in request:
                return
            if request["method"] == "initialize":
                result = {}
            else:
                requests.append(request["params"])
                if len(requests) == 1:
                    result = {"data": [{"id": "astra"}], "nextCursor": "next"}
                else:
                    result = {"data": [{"id": "sol", "serviceTiers": []}],
                              "nextCursor": "next" if repeat_cursor else None}
            replies.feed_data((json.dumps({"id": request["id"], "result": result}) + "\n").encode())

        process = SimpleNamespace(
            stdin=SimpleNamespace(write=write, drain=lambda: asyncio.sleep(0),
                                  close=lambda: closed.append(True)),
            stdout=replies, terminate=lambda: None, wait=lambda: asyncio.sleep(0),
        )

        async def spawn(*_args, **_kwargs):
            return process

        monkeypatch.setattr(models, "_resolve_codex_bin", lambda: "/fake/codex")
        monkeypatch.setattr(models, "_codex_env", lambda *_args: {})
        monkeypatch.setattr(models.asyncio, "create_subprocess_exec", spawn)
        if repeat_cursor:
            with pytest.raises(RuntimeError, match="pagination"):
                await models._rpc_model_list("/isolated/account")
        else:
            rows = models._normalize(await models._rpc_model_list("/isolated/account"))
            assert [row["id"] for row in rows] == ["astra", "sol"]
            assert rows[-1]["service_tiers"] == []
        assert requests == [{"limit": 256}, {"limit": 256, "cursor": "next"}]
        assert closed == [True]

    asyncio.run(run())
