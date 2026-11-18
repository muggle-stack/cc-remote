"""Native model discovery stays fresh on explicit reads and isolated by account."""

import asyncio

import pytest

from cc_remote.wrapper import codex_models as models
from cc_remote.protocol import Models, deserialize, serialize


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch):
    monkeypatch.setattr(models, "_profile_cache", {})
    monkeypatch.setattr(models, "_inflight", {})
    monkeypatch.setattr(models, "codex_current_provider", lambda **_kwargs: "openai")


def ids(catalog):
    return [model["id"] for model in catalog]


def test_reasoning_metadata_preserves_native_order_descriptions_and_default(monkeypatch):
    monkeypatch.setattr(models, "_MAX_CATALOG_TEXT", 16)
    monkeypatch.setattr(models, "_MAX_EFFORTS", 2)
    catalog = models._normalize([{
        "id": "native-model",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "adaptive", "description": "Official adaptive description"},
            {"reasoningEffort": "low", "description": "Official low"},
            {"reasoningEffort": "ultra", "description": "Not included"},
        ],
        "defaultReasoningEffort": "adaptive",
    }])
    assert catalog[0]["efforts"] == ["adaptive", "low"]
    assert catalog[0]["effort_descriptions"] == {
        "adaptive": "Official adaptiv", "low": "Official low",
    }
    assert catalog[0]["default_effort"] == "adaptive"
    message = Models(engine="codex", models=catalog)
    assert deserialize(serialize(message)).models == catalog


@pytest.mark.parametrize("requested,expected", [
    (None, None), ("adaptive", "adaptive"), ("low", "low"),
    ("ultra", "adaptive"), ("retired-effort", "adaptive"),
])
def test_effort_resolution_uses_native_capabilities_and_default(
    monkeypatch, requested, expected,
):
    async def query(_home, *, model_provider=None):
        return [{
            "id": "native-model",
            "supportedReasoningEfforts": [
                {"reasoningEffort": "adaptive"}, {"reasoningEffort": "low"},
            ],
            "defaultReasoningEffort": "adaptive",
        }]

    monkeypatch.setattr(models, "_rpc_model_list", query)

    async def run():
        assert await models.clamp_effort("native-model", requested) == expected
        assert await models.default_effort_for("native-model") == "adaptive"

    asyncio.run(run())


@pytest.mark.parametrize("supported,default", [([], None), (["adaptive"], None), (["adaptive"], "retired")])
def test_unsupported_effort_without_native_default_clears_override(
    monkeypatch, supported, default,
):
    async def query(_home, *, model_provider=None):
        return [{
            "id": "native-model",
            "supportedReasoningEfforts": [{"reasoningEffort": effort} for effort in supported],
            "defaultReasoningEffort": default,
        }]

    monkeypatch.setattr(models, "_rpc_model_list", query)
    assert asyncio.run(models.clamp_effort("native-model", "retired-effort")) is None


def test_effort_resolution_is_account_scoped_and_keeps_unknown_choices(monkeypatch):
    async def query(home, *, model_provider=None):
        if home == "/account/offline":
            raise RuntimeError("offline")
        return [{
            "id": "same-model",
            "supportedReasoningEfforts": [{"reasoningEffort": home.rsplit("/", 1)[-1]}],
            "defaultReasoningEffort": home.rsplit("/", 1)[-1],
        }]

    monkeypatch.setattr(models, "_rpc_model_list", query)

    async def run():
        for account in ("adaptive", "balanced"):
            assert await models.clamp_effort(
                "same-model", "retired", codex_home=f"/account/{account}",
            ) == account
        assert await models.clamp_effort(
            "same-model", "custom", codex_home="/account/offline",
        ) == "custom"
        assert await models.clamp_effort(
            "unlisted", "custom", codex_home="/account/adaptive",
        ) == "custom"

    asyncio.run(run())


@pytest.mark.parametrize("home", [None, "/account/stack"])
def test_explicit_refresh_replaces_fresh_cache_and_updates_internal_lookups(
    monkeypatch, home,
):
    raw = [{"id": "existing"}]
    calls = []

    async def query(codex_home, *, model_provider=None):
        calls.append(codex_home)
        return list(raw)

    monkeypatch.setattr(models, "_rpc_model_list", query)

    async def run():
        assert ids(await models.codex_catalog(codex_home=home)) == ["existing"]
        raw.append({"id": "newly-available"})
        assert ids(await models.codex_catalog(codex_home=home)) == ["existing"]
        assert ids(await models.codex_catalog(force=True, codex_home=home)) == [
            "existing", "newly-available",
        ]
        assert ids(await models.codex_catalog(codex_home=home)) == [
            "existing", "newly-available",
        ]

    asyncio.run(run())
    assert calls == [home, home]


def test_refreshes_share_inflight_query_without_blocking_other_accounts(monkeypatch):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def query(home, *, model_provider=None):
            calls.append(home)
            if home == "/account/slow":
                started.set()
                await release.wait()
            return [{"id": home}]

        monkeypatch.setattr(models, "_rpc_model_list", query)
        first = asyncio.create_task(models.codex_catalog(
            force=True, codex_home="/account/slow"))
        await started.wait()
        second = asyncio.create_task(models.codex_catalog(
            force=True, codex_home="/account/slow/../slow"))
        await asyncio.sleep(0)
        fast = await asyncio.wait_for(models.codex_catalog(
            force=True, codex_home="/account/fast"), timeout=1)
        assert ids(fast) == ["/account/fast"]
        assert not first.done() and not second.done()
        release.set()
        assert ids(await first) == ids(await second) == ["/account/slow"]
        assert calls == ["/account/slow", "/account/fast"]

    asyncio.run(run())


def test_disconnected_reader_does_not_cancel_shared_refresh(monkeypatch):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def query(_home, *, model_provider=None):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return [{"id": "new"}]

        monkeypatch.setattr(models, "_rpc_model_list", query)
        first = asyncio.create_task(models.codex_catalog(force=True, model_provider="openai"))
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(models.codex_catalog(force=True, model_provider="openai"))
        await asyncio.sleep(0)
        release.set()
        assert ids(await second) == ["new"]
        assert ids(await models.codex_catalog(model_provider="openai")) == ["new"]
        assert calls == 1
        assert models._inflight == {}

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["empty", "exception"])
def test_failed_refresh_preserves_only_own_catalog_and_allows_retry(
    monkeypatch, failure,
):
    unavailable = False

    async def query(home, *, model_provider=None):
        if unavailable:
            if failure == "exception":
                raise RuntimeError("native discovery unavailable")
            return []
        return [{"id": home}]

    monkeypatch.setattr(models, "_rpc_model_list", query)

    async def run():
        nonlocal unavailable
        old = await models.codex_catalog(codex_home="/account/one")
        before = dict(models._profile_cache)
        unavailable = True
        assert await models.codex_catalog(force=True, codex_home="/account/one") == old
        assert await models.codex_catalog(force=True, codex_home="/account/two") == []
        assert models._profile_cache == before
        unavailable = False
        assert ids(await models.codex_catalog(force=True, codex_home="/account/two")) == [
            "/account/two",
        ]

    asyncio.run(run())


def test_internal_cache_expires_without_user_refresh(monkeypatch):
    now = 100.0
    calls = 0

    async def query(_home, *, model_provider=None):
        nonlocal calls
        calls += 1
        return [{"id": f"model-{calls}"}]

    monkeypatch.setattr(models, "_rpc_model_list", query)
    monkeypatch.setattr(models.time, "monotonic", lambda: now)

    async def run():
        nonlocal now
        assert ids(await models.codex_catalog()) == ["model-1"]
        now += models._TTL + 1
        assert ids(await models.codex_catalog()) == ["model-2"]

    asyncio.run(run())
