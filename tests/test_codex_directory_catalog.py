import pytest

from cc_remote.wrapper import codex_sessions as module


@pytest.mark.asyncio
async def test_directory_catalog_reads_every_page_and_both_archive_states(monkeypatch):
    calls = []
    monkeypatch.setattr(module, "codex_current_provider", lambda **_: "company")

    async def rpc(method, params, **kwargs):
        calls.append((method, params.copy(), kwargs))
        offset = int(params.get("cursor", "0"))
        count = 121 if params["archived"] else 223
        return {"data": [{"id": f'{params["archived"]}-{i}', "cwd": "/project"}
                         for i in range(offset, min(offset + 100, count))],
                "nextCursor": str(offset + 100) if offset + 100 < count else None}

    monkeypatch.setattr(module, "codex_rpc", rpc)
    rows = await module.list_codex_directory_sessions("/project", codex_home="/profile")
    assert len(rows) == 344 and sum(row["tag"] == "archived" for row in rows) == 121
    assert len(calls) == 5
    assert all(method == "thread/list" and params["cwd"] == "/project"
               and params["modelProviders"] == ["company"] and params["useStateDbOnly"]
               and kwargs == {"codex_home": "/profile"} for method, params, kwargs in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["overflow", "cursor-loop", "wrong-directory", "unavailable"])
async def test_incomplete_directory_catalog_never_returns_a_partial_inventory(monkeypatch, fault):
    monkeypatch.setattr(module, "codex_current_provider", lambda **_: "")

    async def rpc(*args, **kwargs):
        if fault == "unavailable":
            return {"error": "unavailable"}
        return {"data": [{"id": str(i), "cwd": "/other" if fault == "wrong-directory" else "/project"}
                         for i in range(3)], "nextCursor": "same" if fault == "cursor-loop" else None}

    monkeypatch.setattr(module, "codex_rpc", rpc)
    with pytest.raises((ValueError, RuntimeError)):
        await module.list_codex_directory_sessions("/project", limit=2 if fault == "overflow" else 1000)


@pytest.mark.asyncio
async def test_mutation_refresh_uses_index_without_changing_normal_discovery(monkeypatch):
    calls = []
    monkeypatch.setattr(module, "codex_current_provider", lambda **_: "")

    async def rpc(method, params, **kwargs):
        calls.append(params)
        return {"data": []}

    monkeypatch.setattr(module, "codex_rpc", rpc)
    with module.codex_catalog_read_mode(index_only=True):
        await module.list_codex_sessions()
    await module.list_codex_sessions()
    assert all(params["useStateDbOnly"] for params in calls[:2])
    assert all("useStateDbOnly" not in params for params in calls[2:])
