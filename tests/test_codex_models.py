"""Native model discovery stays fresh on explicit reads and isolated by account."""

import asyncio

import pytest

from cc_remote.wrapper import codex_models as models


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch):
    monkeypatch.setattr(models, "_profile_cache", {})
    monkeypatch.setattr(models, "_inflight", {})


def ids(catalog):
    return [model["id"] for model in catalog]


@pytest.mark.parametrize("home", [None, "/account/stack"])
def test_explicit_refresh_replaces_fresh_cache_and_updates_internal_lookups(
    monkeypatch, home,
):
    raw = [{"id": "existing"}]
    calls = []

    async def query(codex_home):
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

        async def query(home):
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

        async def query(_home):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return [{"id": "new"}]

        monkeypatch.setattr(models, "_rpc_model_list", query)
        first = asyncio.create_task(models.codex_catalog(force=True))
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(models.codex_catalog(force=True))
        await asyncio.sleep(0)
        release.set()
        assert ids(await second) == ["new"]
        assert ids(await models.codex_catalog()) == ["new"]
        assert calls == 1
        assert models._inflight == {}

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["empty", "exception"])
def test_failed_refresh_preserves_only_own_catalog_and_allows_retry(
    monkeypatch, failure,
):
    unavailable = False

    async def query(home):
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

    async def query(_home):
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
