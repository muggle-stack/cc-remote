"""Zero-model static Viewer security, HTTP and real binary-channel tests."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from types import SimpleNamespace
import re
import socket
import stat
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from cc_remote.config import RelayConfig
from cc_remote.protocol import PROTOCOL_VERSION
from cc_remote.relay.server import create_app
from cc_remote.relay.viewer import (
    MAX_GRANTS, ViewerError, main_cookie_name, validate_origin_template,
    effective_viewer_mode,
)
from cc_remote.viewer import (
    CHUNK_SIZE, MAX_REQUESTS, ViewerSite, clean_path, edit_sites, load_sites, open_resource,
    resource_headers,
)
from cc_remote.wrapper.viewer_transport import ViewerTransport
from cc_remote.wrapper.viewer_pages import SessionPages
from cc_remote.viewer_pages import PageScope, PageRef, ViewerPageStore, locate_pages, verify_page


@pytest.fixture(autouse=True)
def isolated_login_limiter():
    from cc_remote.relay.server import _login_limiter
    _login_limiter.reset()
    yield
    _login_limiter.reset()


@pytest.fixture
def publication(tmp_path):
    root = tmp_path / "project"
    (root / "viewer").mkdir(parents=True)
    (root / "meshes").mkdir()
    (root / "viewer/index.html").write_text(
        '<!doctype html><script type="module" src="./app.js"></script><p>Viewer</p>')
    (root / "viewer/app.js").write_text('fetch("../meshes/part.stl?v=2")')
    (root / "meshes/part.stl").write_bytes(bytes(range(256)) * 1024)
    (root / "private.json").write_text('{"secret":"not published"}')
    info = root.stat()
    site = ViewerSite(id="robot", label="Robot Viewer", root=str(root),
                      root_device=info.st_dev, root_inode=info.st_ino,
                      entry="/viewer/index.html", paths=["/viewer/", "/meshes/"],
                      script_origins=["https://esm.sh"],
                      urls=["http://localhost:9000/viewer/index.html"])
    registry = tmp_path / "viewers.json"
    registry.write_text(json.dumps({"sites": [site.model_dump()]}))
    registry.chmod(0o600)
    return site, registry


@pytest.mark.parametrize("path", [
    "/../private.json", "/viewer/../private.json", "/viewer/%2e%2e/private.json",
    "/viewer/%252e%252e/private.json", "//viewer/app.js", "/viewer//app.js",
    "/viewer\\app.js", "/viewer/%00.js", "/viewer/app.js?bad", "viewer/app.js",
    "/viewer/.env", "/viewer/%ff", "/viewer/%zz", "/viewer/%5capp.js",
])
def test_viewer_rejects_ambiguous_paths(path):
    with pytest.raises((ValueError, UnicodeError)):
        clean_path(path)


def test_publication_stays_specific_and_hides_private_paths(publication):
    site, registry = publication
    assert load_sites(registry)[site.id] == site
    assert "root" not in site.public()
    for path in ("/private.json", "/viewer/auth.py", "/viewer2/app.js"):
        with pytest.raises((PermissionError, FileNotFoundError)):
            open_resource(site, path)
    with pytest.raises(ValueError):
        ViewerSite.model_validate({**site.model_dump(), "paths": ["/"]})
    with pytest.raises(ValueError):
        ViewerSite.model_validate({**site.model_dump(), "script_origins": ["https://evil.example"]})
    registry.chmod(0o644)
    with pytest.raises(ValueError):
        load_sites(registry)


def test_symlinks_hardlinks_and_root_replacement_fail_closed(publication, tmp_path):
    site, _ = publication
    root = Path(site.root)
    (root / "viewer/link.json").symlink_to(root / "private.json")
    (root / "viewer/linked-dir").symlink_to(root, target_is_directory=True)
    for path in ("/viewer/link.json", "/viewer/linked-dir/private.json"):
        with pytest.raises(OSError):
            open_resource(site, path)
    os.link(root / "private.json", root / "viewer/hard.json")
    with pytest.raises(PermissionError):
        open_resource(site, "/viewer/hard.json")
    root.rename(tmp_path / "previous")
    root.mkdir()
    with pytest.raises(PermissionError):
        open_resource(site, "/viewer/index.html")


def test_file_descriptor_is_pinned_after_open(publication):
    site, _ = publication
    stream, info, mime = open_resource(site, "/viewer/app.js")
    assert stat.S_ISREG(info.st_mode)
    assert mime == "text/javascript"
    path = Path(site.root) / "viewer/app.js"
    path.rename(path.with_suffix(".old"))
    path.symlink_to(Path(site.root) / "private.json")
    with stream:
        assert b"fetch" in stream.read()


def test_registry_edits_preserve_previous_snapshot_on_failure(publication):
    site, registry = publication
    with pytest.raises(RuntimeError), edit_sites(registry) as sites:
        sites.clear()
        raise RuntimeError("failed registration")
    assert load_sites(registry) == {site.id: site}
    with edit_sites(registry) as sites:
        sites["other"] = site.model_copy(update={"id": "other"})
    assert set(load_sites(registry)) == {site.id, "other"}
    assert stat.S_IMODE(registry.stat().st_mode) == 0o600


@pytest.mark.parametrize("value,expected", [
    ("bytes=0-3", (206, 0, 4)), ("bytes=100-", (206, 100, 262044)),
    ("bytes=-5", (206, 262139, 5)), ("bytes=999999-", (416, 0, 0)),
    ("bytes=-0", (416, 0, 0)), ("bytes=4-2", (416, 0, 0)),
    ("bytes=0-1,3-4", (416, 0, 0)), ("invalid", (416, 0, 0)),
])
def test_static_byte_ranges(publication, value, expected):
    site, _ = publication
    with open_resource(site, "/meshes/part.stl")[0] as stream:
        info = os.fstat(stream.fileno())
        status, _, start, length = resource_headers(info, "model/stl", {"range": value})
        assert (status, start, length) == expected


@pytest.mark.parametrize("template", [
    "https://preview.example", "https://{id}.preview.example/path",
    "https://{id}.preview.example?token=x", "https://u:p@{id}.preview.example",
    "http://{id}.preview.example", "https://{id}.{id}.example",
])
def test_invalid_preview_host_templates(template):
    with pytest.raises(ValueError):
        validate_origin_template(template, "https://remote.example")


def cfg_for(tmp_path, **values):
    return RelayConfig(login_password="correct horse battery staple", session_secret="s" * 48,
                       wrapper_token="w" * 48, device_db_path=str(tmp_path / "devices.sqlite3"),
                       **values)


def test_viewer_opt_in_uses_host_only_main_session_cookie(tmp_path):
    cfg = cfg_for(tmp_path, public_origin="https://app.example.com",
                  viewer_origin_template="https://{id}.preview.example.com")
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        login = client.post("/api/login", json={"password": cfg.login_password})
        assert login.status_code == 200
        header = login.headers["set-cookie"]
        assert "__Host-cc_remote_session=" in header
        assert "Secure" in header and "HttpOnly" in header and "Domain" not in header
        assert client.get("/api/session").status_code == 200
        assert main_cookie_name(cfg) == "__Host-cc_remote_session"
        # A legacy/sibling Domain cookie is not accepted when viewers are enabled.
        token = login.cookies[main_cookie_name(cfg)]
        client.cookies.clear()
        assert client.get("/api/session", headers={"Cookie": f"cc_remote_session={token}"}).status_code == 401


def test_viewer_opt_in_preserves_private_http_login(tmp_path):
    cfg = cfg_for(tmp_path, public_origin="https://app.example.com", port=8777,
                  allow_private_origins=True,
                  viewer_origin_template="https://{id}.preview.example.com")
    with TestClient(create_app(cfg), base_url="http://127.0.0.1:8777") as client:
        login = client.post("/api/login", json={"password": cfg.login_password},
                            headers={"Origin": "http://127.0.0.1:8777"})
        assert login.status_code == 200
        assert "cc_remote_session=" in login.headers["set-cookie"]
        assert "__Host-" not in login.headers["set-cookie"]
        assert client.get("/api/session").status_code == 200


@asynccontextmanager
async def live_viewer(tmp_path, publication, mode="isolated", home_pages=None):
    site, registry = publication
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    cfg = cfg_for(tmp_path, public_origin=f"http://127.0.0.1:{port}" if mode == "bridge" else f"http://localhost:{port}",
                  viewer_mode=mode,
                  viewer_origin_template=f"http://{{id}}.localhost:{port}")
    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    async def ready():
        while not server.started:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(ready(), 5)
    async def scope_context(scope):
        return scope, site.root
    pages = SessionPages(tmp_path / "session-pages.json", scope_context)
    app.state.viewer_page_store = pages.store
    transport = ViewerTransport(f"ws://127.0.0.1:{port}/ws", cfg.wrapper_token, "device", registry,
                                session_pages=pages, home_pages=home_pages)
    wrapper = asyncio.create_task(transport.run())
    async def connected():
        while "device" not in app.state.viewers.peers:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(connected(), 5)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url=cfg.public_origin) as client:
            login = await client.post("/api/login", json={"password": cfg.login_password})
            assert login.status_code == 200
            yield client, app, cfg, site
    finally:
        wrapper.cancel()
        await asyncio.gather(wrapper, return_exceptions=True)
        server.should_exit = True
        await asyncio.wait_for(server_task, 5)
        listener.close()


def test_page_store_persists_deduplicates_and_scopes(tmp_path):
    store = ViewerPageStore(tmp_path / "state" / "pages.json")
    scope = PageScope(sid="profile:one:session", engine="codex", space="code")
    page = PageRef(machine_id="source-device", site_id="demo", entry="/pages/index.html",
                   label="Demo", references=["/project/pages/index.html"], turn_ids=["turn-1"])
    store.associate(scope, [page], automatic=True)
    store.associate(scope, [page.model_copy(update={"turn_ids": ["turn-2"]})], automatic=True)
    rows = ViewerPageStore(store.path).list(scope)
    assert len(rows) == 1
    assert rows[0]["turn_ids"] == ["turn-1", "turn-2"]
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    for update in ({"sid": "profile:two:session"}, {"engine": "claude"}, {"space": "work"}):
        assert store.list(scope.model_copy(update=update)) == []
    store.remove(scope, page.id)
    assert store.associate(scope, [page], automatic=True) == []
    assert len(store.associate(scope, [page], automatic=False)) == 1
    store.rekey("codex", "code", scope.sid, "real-session")
    assert store.list(scope) == []
    real = scope.model_copy(update={"sid": "real-session"})
    assert len(store.list(real)) == 1
    assert store.list(scope.model_copy(update={"sid": "fork-session"})) == []
    store.drop("codex", real.sid)
    assert store.list(real) == []


def test_page_store_rejects_insecure_and_oversized_state(tmp_path):
    path = tmp_path / "pages.json"
    path.write_text("{}")
    path.chmod(0o644)
    scope = PageScope(sid="session", engine="codex", space="code")
    with pytest.raises(ValueError):
        ViewerPageStore(path).list(scope)
    path.chmod(0o600)
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    with pytest.raises(OSError):
        ViewerPageStore(alias).list(scope)
    store = ViewerPageStore(path)
    pages = [PageRef(machine_id="device", site_id=f"page-{i}", entry="/index.html", label="Page") for i in range(33)]
    with pytest.raises(ValueError):
        store.associate(scope, pages, automatic=True)
    assert store.list(scope) == []  # failed transaction is atomic


def test_page_profile_migration_merges_tombstones_and_keeps_engine_revisions(tmp_path):
    store = ViewerPageStore(tmp_path / "pages.json")
    old = PageScope(engine="claude", space="code", sid="session")
    qualified = old.model_copy(update={"sid": "personal@session"})
    codex = old.model_copy(update={"engine": "codex"})
    page = PageRef(machine_id="device", site_id="demo", entry="/index.html", label="Demo",
                   references=["old.html"], turn_ids=["old-turn"])
    store.associate(old, [page], automatic=True)
    store.associate(qualified, [page.model_copy(update={"references": ["new.html"],
                                                       "turn_ids": ["new-turn"]})], automatic=True)
    store.remove(qualified, page.id)
    store.associate(codex, [page], automatic=False)
    store.migrate_profile_sessions("claude", lambda sid: "personal@session", profile_revision=1)
    assert store.list(old) == []
    assert store.associate(qualified, [page], automatic=True) == []
    assert store.list(codex) == [page.public()]
    assert store.associate(qualified, [page], automatic=False)[0]["turn_ids"] == ["old-turn", "new-turn"]
    store.migrate_profile_sessions("codex", lambda sid: "stack@" + sid, profile_revision=1)
    assert store.list(codex) == []
    assert store.list(codex.model_copy(update={"sid": "stack@session"})) == [page.public()]
    before = store.path.read_bytes()
    reloaded = ViewerPageStore(store.path)
    reloaded.migrate_profile_sessions("codex", lambda sid: "wrong@" + sid, profile_revision=1)
    reloaded.migrate_profile_sessions("claude", lambda sid: "wrong@" + sid, profile_revision=1)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("failure", ["invalid_sid", "too_many_pages", "too_many_bytes"])
def test_page_profile_migration_failure_preserves_rows_and_revision(tmp_path, failure):
    store = ViewerPageStore(tmp_path / "pages.json")
    scopes = [PageScope(engine="claude", space="code", sid=sid) for sid in ("a", "b")]
    for i, scope in enumerate(scopes):
        if failure == "too_many_pages":
            pages = [PageRef(machine_id="device", site_id=f"page-{i}-{j}", entry="/index.html",
                             label="Demo") for j in range(17)]
        elif failure == "too_many_bytes":
            pages = [PageRef(machine_id="device", site_id=f"page-{i}", entry="/index.html", label="Demo",
                             references=[str(j) * 4000 for j in range(8)])]
        else:
            pages = [PageRef(machine_id="device", site_id="demo", entry="/index.html", label="Demo")]
        store.associate(scope, pages, automatic=False)
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.migrate_profile_sessions("claude", lambda sid: "" if failure == "invalid_sid" else "merged",
                                       profile_revision=1)
    assert store.path.read_bytes() == before
    store.migrate_profile_sessions("claude", lambda sid: "profile@" + sid, profile_revision=1)
    assert all(store.list(scope.model_copy(update={"sid": "profile@" + scope.sid})) for scope in scopes)


@pytest.mark.parametrize("revisions", [{"claude": True}, {"codex": -1}, {"other": 1}, []])
def test_page_store_rejects_invalid_profile_revision_metadata(tmp_path, revisions):
    path = tmp_path / "pages.json"
    path.write_text(json.dumps({"_profile_revisions": revisions}))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="profile revisions"):
        ViewerPageStore(path).list(PageScope(engine="claude", space="code", sid="session"))


def test_page_profile_metadata_does_not_consume_a_session_slot(tmp_path, monkeypatch):
    monkeypatch.setattr("cc_remote.viewer_pages.MAX_SCOPES", 1)
    store = ViewerPageStore(tmp_path / "pages.json")
    store.migrate_profile_sessions("claude", lambda sid: sid, profile_revision=1)
    scope = PageScope(engine="claude", space="code", sid="session")
    page = PageRef(machine_id="device", site_id="demo", entry="/index.html", label="Demo")
    assert store.associate(scope, [page], automatic=False) == [page.public()]
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="page store full"):
        store.associate(scope.model_copy(update={"sid": "another"}), [page], automatic=False)
    assert store.path.read_bytes() == before
    store.rekey("claude", "code", scope.sid, "real-session")
    assert store.list(scope.model_copy(update={"sid": "real-session"})) == [page.public()]
    store.drop("claude", "real-session")
    assert store.list(scope.model_copy(update={"sid": "real-session"})) == []


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_viewer_migration_failure_preserves_chat_and_replay_marker(tmp_path, monkeypatch, engine):
    from cc_remote.config import WrapperConfig
    from cc_remote.wrapper.machine import WrapperMachine

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-a"))
    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.claude_profiles_json = json.dumps({
        "a": {"label": "A", "config_dir": str(tmp_path / "claude-a"), "default": True},
        "b": {"label": "B", "config_dir": str(tmp_path / "claude-b")},
    })
    cfg.codex_profiles_json = json.dumps({
        "a": {"label": "A", "home": str(tmp_path / "codex-a"), "default": True},
        "b": {"label": "B", "home": str(tmp_path / "codex-b")},
    })
    original = ViewerPageStore.migrate_profile_sessions

    def fail_one(self, row_engine, *args, **kwargs):
        if row_engine == engine:
            raise OSError("temporary write failure")
        return original(self, row_engine, *args, **kwargs)

    monkeypatch.setattr(ViewerPageStore, "migrate_profile_sessions", fail_one)
    machine = WrapperMachine(cfg, SimpleNamespace(on_connected=None))
    assert machine._claude_profile_migration_ok
    assert machine._codex_profile_migration_ok
    assert machine._claude_work_profile_migration_ok
    assert machine._codex_work_profile_migration_ok
    assert machine.viewer_pages.blocked_engines == {engine}
    marker = cfg.state_dir / f"{engine}-profile-transition.json"
    assert marker.exists()
    scope = PageScope(engine=engine, space="code", sid="a@session")

    async def blocked_operations():
        # No old-key operation may race current-topology writes while recovery
        # is pending, including lifecycle mutations that bypass resolve_scope.
        for payload in (
            {"operation": "list"},
            {"operation": "associate", "pages": [], "automatic": True},
            {"operation": "remove", "page_id": "0" * 32},
        ):
            with pytest.raises(ValueError, match="profile migration is incomplete"):
                await machine.viewer_pages({"scope": scope.model_dump(), **payload})
        with pytest.raises(ValueError, match="profile migration is incomplete"):
            await machine.viewer_pages.rekey(engine, "code", scope.sid, "new-session")
        with pytest.raises(ValueError, match="profile migration is incomplete"):
            await machine.viewer_pages.drop(engine, scope.sid)

    asyncio.run(blocked_operations())
    monkeypatch.setattr(ViewerPageStore, "migrate_profile_sessions", original)
    recovered = WrapperMachine(cfg, SimpleNamespace(on_connected=None))
    assert recovered.viewer_pages.blocked_engines == set()
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["rekey", "drop"])
async def test_cancelled_page_write_finishes_before_session_lifecycle(tmp_path, monkeypatch, lifecycle):
    async def scope_context(scope):
        return scope, "/project"
    service = SessionPages(tmp_path / "pages.json", scope_context)
    scope = PageScope(sid="tmp-session", engine="codex", space="code")
    page = PageRef(machine_id="device", site_id="demo", entry="/index.html", label="Demo")
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = service.store.associate

    def slow_associate(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        if not release.wait(5):
            raise TimeoutError("test did not release the page write")
        return original(*args, **kwargs)

    monkeypatch.setattr(service.store, "associate", slow_associate)
    writing = asyncio.create_task(service({"scope": scope.model_dump(), "operation": "associate",
                                           "pages": [page.model_dump()], "automatic": True}))
    try:
        await asyncio.wait_for(started.wait(), 3)
        writing.cancel()
        await asyncio.sleep(0)
        operation = (service.rekey("codex", "code", scope.sid, "real-session") if lifecycle == "rekey"
                     else service.drop("codex", scope.sid))
        changing = asyncio.create_task(operation)
        await asyncio.sleep(0)
        writing.cancel()  # transport and request teardown may both cancel
        await asyncio.sleep(0)
        assert not writing.done()
        assert not changing.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await writing
    await asyncio.wait_for(changing, 3)
    assert service.store.list(scope) == []
    real_rows = service.store.list(scope.model_copy(update={"sid": "real-session"}))
    assert len(real_rows) == (1 if lifecycle == "rekey" else 0)


def test_page_discovery_never_expands_publication_or_follows_symlinks(publication):
    site, registry = publication
    root = Path(site.root)
    (root / "private.html").write_text("private")
    (root / "viewer/link.html").symlink_to(root / "viewer/index.html")
    (root / "viewer/link-dir").symlink_to(root / "viewer")
    paths = [str(root / "viewer/index.html"), str(root / "private.html"),
             str(root / "viewer/link.html"), str(root / "viewer/link-dir/index.html"),
             "https://github.com/demo/index.html", "https://example.com/index.html",
             "http://127.0.0.1:8000/index.html", "viewer/index.html"]
    values = locate_pages(registry, paths)
    assert [p["reference"] for p in values] == [paths[0]]
    assert load_sites(registry)[site.id] == site
    before = values[0]["content_revision"]
    (root / "viewer/index.html").write_text("updated page")
    assert locate_pages(registry, [paths[0]])[0]["content_revision"] != before
    with edit_sites(registry) as sites:
        sites["duplicate"] = site.model_copy(update={"id": "duplicate"})
    assert locate_pages(registry, [paths[0]]) == []


@pytest.mark.asyncio
async def test_session_pages_api_persists_resolves_removes_and_opens_alternate_entry(tmp_path, publication):
    site, registry = publication
    entry = Path(site.root) / "viewer/second.html"
    entry.write_text("<p>Second page</p>")
    async with live_viewer(tmp_path, publication, mode="bridge") as (client, app, cfg, _):
        scope = {"machine_id": "device", "sid": "session-a", "engine": "codex", "space": "code"}
        async def call(action, **kwargs):
            response = await client.post("/api/viewers/pages", headers={"Origin": cfg.public_origin},
                                         json={"action": action, "scope": scope, **kwargs})
            assert response.status_code == 200, response.text
            return response.json()["pages"]
        assert await call("list") == []
        assert await call("resolve", paths=["https://github.com/demo/index.html", "http://localhost:8000/index.html"], turn_id="turn") == []
        rows = await call("resolve", paths=["viewer/second.html"], turn_id="turn")
        assert len(rows) == 1 and rows[0]["available"] is True
        assert rows[0]["entry"] == "/viewer/second.html"
        stable_id = rows[0]["id"]
        assert (await call("resolve", paths=[str(entry)], turn_id="turn-2"))[0]["id"] == stable_id
        persisted = ViewerPageStore(tmp_path / "session-pages.json")
        assert len(persisted.list(PageScope(sid="session-a", engine="codex", space="code"))) == 1
        scope["sid"] = "session-b"
        assert await call("list") == []
        scope["sid"] = "session-a"
        grant = await create_grant(client, cfg, entry="/viewer/second.html")
        assert grant["entry"] == "/viewer/second.html"
        # Binding a new entry never rewrites the publication or widens its paths.
        assert load_sites(registry)[site.id] == site
        assert await call("remove", page_id=stable_id) == []
        assert await call("resolve", paths=[str(entry)], turn_id="turn-3") == []
        assert entry.exists()
        rows = await call("associate", page={"machine_id": "device", "site_id": "robot", "entry": "/viewer/second.html"})
        assert len(rows) == 1
        entry.unlink()
        assert (await call("list"))[0]["available"] is False
        rejected = await client.post("/api/viewers/pages", headers={"Origin": cfg.public_origin}, json={
            "scope": scope, "action": "associate", "page": {"machine_id": "device", "site_id": "robot", "entry": "/private.html"}})
        assert rejected.status_code == 404
        no_origin = await client.post("/api/viewers/pages", json={"scope": scope, "action": "list"})
        assert no_origin.status_code == 403


@pytest.mark.asyncio
async def test_page_metadata_routes_to_source_device_and_does_not_guess(tmp_path, publication):
    from cc_remote.relay.viewer_pages import page_api
    site, registry = publication
    async def scope_context(scope):
        return scope, "/parent/workspace"
    service = SessionPages(tmp_path / "parent-pages.json", scope_context)
    class Peer:
        closed = False
        session_pages = True
        sites = {}
        async def metadata(self, action, payload):
            if action in {"locate", "verify"}:
                return []
            assert action == "session"
            return await service(payload)
    class Source(Peer):
        sites = {site.id: site}
        async def metadata(self, action, payload):
            if action == "locate":
                return locate_pages(registry, payload["paths"])
            assert action == "verify"
            return [verify_page(site, p["entry"]) for p in payload["entries"]]
    allowed = {"parent", "source"}
    async def allow(_claims, mid):
        return mid in allowed
    async def active(_claims):
        return True
    relay = SimpleNamespace(peers={"parent": Peer(), "source": Source()}, allow_machine=allow, session_active=active)
    payload = {"scope": {"machine_id": "parent", "sid": "session", "engine": "codex", "space": "code"},
               "action": "resolve", "paths": [str(Path(site.root) / "viewer/index.html")], "turn_id": "turn"}
    result = await page_api(relay, object(), payload)
    assert len(result["pages"]) == 1
    assert result["pages"][0]["machine_id"] == "source"
    assert result["pages"][0]["available"]
    allowed.add("duplicate")
    relay.peers["duplicate"] = Source()
    payload["scope"]["sid"] = "ambiguous"
    assert (await page_api(relay, object(), payload))["pages"] == []
    # Explicit association can resolve ambiguity, but cannot grant a new path.
    manual = {"scope": payload["scope"], "action": "associate", "page": {
        "machine_id": "source", "site_id": "robot", "entry": "/viewer/index.html"}}
    assert len((await page_api(relay, object(), manual))["pages"]) == 1
    allowed.remove("source")
    assert (await page_api(relay, object(), {"scope": payload["scope"], "action": "list"}))["pages"] == []
    with pytest.raises(ViewerError) as exc:
        await page_api(relay, object(), manual)
    assert exc.value.status == 403


@pytest.mark.asyncio
async def test_live_page_observer_only_binds_published_html(tmp_path, publication, monkeypatch):
    from cc_remote.wrapper.machine import WrapperMachine
    site, registry = publication
    monkeypatch.setattr("cc_remote.viewer.registry_path", lambda: registry)
    async def scope_context(scope):
        return scope, site.root
    service = SessionPages(tmp_path / "observer-pages.json", scope_context)
    machine = SimpleNamespace(cfg=SimpleNamespace(machine_id="device", state_dir=tmp_path), viewer_pages=service,
                              _ctx_wire_sid=lambda ctx: ctx.key)
    ctx = SimpleNamespace(engine="codex", space="code", key="tmp-session", cwd=site.root,
                          active_msg_id="turn", btw=False)
    await WrapperMachine._observe_viewer_page_paths(machine, ctx, ["viewer/index.html", "/private/index.html"])
    scope = PageScope(sid=ctx.key, engine="codex", space="code")
    rows = service.store.list(scope)
    assert len(rows) == 1 and rows[0]["turn_ids"] == ["turn"]
    await service.rekey("codex", "code", "tmp-session", "real-session")
    assert service.store.list(scope) == []
    assert len(service.store.list(scope.model_copy(update={"sid": "real-session"}))) == 1
    ctx.btw = True
    ctx.key = "side-chat"
    await WrapperMachine._observe_viewer_page_paths(machine, ctx, ["viewer/index.html"])
    assert service.store.list(scope.model_copy(update={"sid": ctx.key})) == []


async def create_grant(client, cfg, **overrides):
    response = await client.post("/api/viewers/open", headers={"Origin": cfg.public_origin},
                                 json={"machine_id": "device", "site_id": "robot",
                                       "parent_machine_id": "device", "sid": "session-a", **overrides})
    assert response.status_code == 200, response.text
    return response.json()


def bridge_socket(client, cfg, **overrides):
    headers = {"Cookie": "; ".join(f"{key}={value}" for key, value in client.cookies.items())}
    headers.update(overrides.pop("headers", {}))
    return connect(cfg.public_origin.replace("http", "ws", 1) + "/ws/viewer-client",
                   origin=overrides.pop("origin", cfg.public_origin), additional_headers=headers,
                   proxy=None, **overrides)


async def bind_bridge(ws, grant):
    await ws.send(json.dumps({"type": "bind", "v": PROTOCOL_VERSION, "id": grant["id"]}))
    return json.loads(await asyncio.wait_for(ws.recv(), 3))


def test_bridge_default_preserves_main_cookie_and_explicit_legacy_configuration(tmp_path):
    cfg = cfg_for(tmp_path)
    assert effective_viewer_mode(cfg) == "bridge"
    assert main_cookie_name(cfg) == "cc_remote_session"
    cfg.viewer_origin_template = "https://{id}.preview.example"
    assert effective_viewer_mode(cfg) == "isolated"
    cfg.viewer_mode = "bridge"
    assert effective_viewer_mode(cfg) == "bridge"
    cfg.viewer_mode = "off"
    assert effective_viewer_mode(cfg) == "off"


@pytest.mark.asyncio
async def test_bridge_real_binary_reads_no_unsolicited_bytes_and_revocation(tmp_path, publication):
    async with live_viewer(tmp_path, publication, "bridge") as (client, app, cfg, site):
        grant = await create_grant(client, cfg)
        assert grant["mode"] == "bridge" and "origin" not in grant and "token" not in str(grant)
        runner = await client.get(grant["runner"])
        assert runner.status_code == 200
        csp = runner.headers["content-security-policy"]
        assert "sandbox allow-scripts;" in csp and "allow-same-origin" not in csp
        assert "connect-src 'none'" in csp and "frame-ancestors 'self'" in csp
        assert "root" not in runner.text and "app.js" not in runner.text
        async with bridge_socket(client, cfg) as ws:
            assert (await bind_bridge(ws, grant))["type"] == "bound"
            request_id = "a" * 32
            await ws.send(json.dumps({"type": "read", "request_id": request_id,
                                      "path": "/meshes/part.stl", "method": "GET", "headers": {}}))
            metadata = json.loads(await asyncio.wait_for(ws.recv(), 3))
            assert metadata["status"] == 200
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ws.recv(), .05)
            await ws.send(json.dumps({"type": "pull", "request_id": request_id, "credits": 8}))
            chunks = []
            while True:
                message = await asyncio.wait_for(ws.recv(), 3)
                if isinstance(message, str):
                    assert json.loads(message)["type"] == "end"
                    break
                assert message[:16] == bytes.fromhex(request_id) and len(message) <= CHUNK_SIZE + 16
                chunks.append(message[16:])
            assert b"".join(chunks) == (Path(site.root) / "meshes/part.stl").read_bytes()
            await client.delete(f'/api/viewers/{grant["id"]}', headers={"Origin": cfg.public_origin})
            error = json.loads(await asyncio.wait_for(ws.recv(), 3))
            assert error["type"] == "error" and error["status"] in {401, 410}
        await asyncio.sleep(.05)
        assert not app.state.viewers.bridges and not app.state.viewers.peers["device"].pending
        assert not app.state.hub.machine_ids


@pytest.mark.asyncio
async def test_bridge_auth_origin_jti_duplicate_socket_and_path_boundary(tmp_path, publication):
    async with live_viewer(tmp_path, publication, "bridge") as (client, app, cfg, site):
        grant = await create_grant(client, cfg)
        for override in [{"origin": "null"}, {"origin": "http://evil.example"},
                         {"origin": None}, {"headers": {"Cookie": ""}},
                         {"headers": {"Authorization": "Bearer " + cfg.wrapper_token}}]:
            with pytest.raises(InvalidStatus):
                async with bridge_socket(client, cfg, **override):
                    pytest.fail("unauthorized connection accepted")
        async with bridge_socket(client, cfg) as ws:
            assert (await bind_bridge(ws, grant))["type"] == "bound"
            async with bridge_socket(client, cfg) as duplicate:
                assert (await bind_bridge(duplicate, grant))["status"] == 403
            assert grant["id"] in app.state.viewers.bridges
            for index, path in enumerate(["/private.json", "/viewer/%2e%2e/private.json", "/api/session"]):
                identity = f"{index + 1:032x}"
                await ws.send(json.dumps({"type": "read", "request_id": identity, "path": path}))
                error = json.loads(await asyncio.wait_for(ws.recv(), 3))
                assert error["type"] == "failed" and error["status"] in {400, 403}
            await client.post("/api/login", json={"password": cfg.login_password})
            async with bridge_socket(client, cfg) as different_login:
                assert (await bind_bridge(different_login, grant))["status"] == 403
            await ws.send(json.dumps({"type": "ping"}))
            assert json.loads(await ws.recv())["type"] == "pong"


@pytest.mark.asyncio
async def test_bridge_range_head_cache_cancel_and_logout(tmp_path, publication):
    async with live_viewer(tmp_path, publication, "bridge") as (client, app, cfg, site):
        grant = await create_grant(client, cfg)
        async with bridge_socket(client, cfg) as ws:
            await bind_bridge(ws, grant)
            for index, (method, headers, status, size) in enumerate([
                ("GET", {"range": "bytes=2-7"}, 206, 6),
                ("HEAD", {}, 200, 0), ("GET", {"range": "bytes=999999-"}, 416, 0),
            ]):
                identity = f"{index + 1:032x}"
                await ws.send(json.dumps({"type": "read", "request_id": identity,
                                          "path": "/meshes/part.stl", "method": method, "headers": headers}))
                metadata = json.loads(await ws.recv())
                assert metadata["status"] == status
                if size:
                    await ws.send(json.dumps({"type": "pull", "request_id": identity, "credits": 1}))
                    assert (await ws.recv())[16:] == bytes(range(2, 8))
                assert json.loads(await ws.recv())["type"] == "end"
            for index in range(5, 9):
                identity = f"{index:032x}"
                await ws.send(json.dumps({"type": "read", "request_id": identity, "path": "/meshes/part.stl"}))
                assert json.loads(await ws.recv())["type"] == "response"
                await ws.send(json.dumps({"type": "cancel", "request_id": identity}))
            await ws.send(json.dumps({"type": "ping"}))
            assert json.loads(await ws.recv())["type"] == "pong"
            assert not app.state.viewers.peers["device"].pending
            await client.post("/api/logout", headers={"Origin": cfg.public_origin})
            assert json.loads(await asyncio.wait_for(ws.recv(), 3))["status"] == 401


@pytest.mark.asyncio
async def test_bridge_head_releases_source_slot_before_completion_receipt(tmp_path, publication, monkeypatch):
    async with live_viewer(tmp_path, publication, "bridge") as (client, app, cfg, site):
        grant = await create_grant(client, cfg)
        peer = app.state.viewers.peers["device"]
        original_cancel = peer.cancel
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_cancel(identity):
            await original_cancel(identity)
            entered.set()
            await release.wait()

        monkeypatch.setattr(peer, "cancel", slow_cancel)
        async with bridge_socket(client, cfg) as ws:
            await bind_bridge(ws, grant)
            await ws.send(json.dumps({"type": "read", "request_id": "a" * 32,
                                      "path": "/meshes/part.stl", "method": "HEAD"}))
            assert json.loads(await ws.recv())["type"] == "response"
            try:
                await asyncio.wait_for(entered.wait(), 3)
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(ws.recv(), .05)
            finally:
                release.set()
            assert json.loads(await asyncio.wait_for(ws.recv(), 3))["type"] == "end"
            assert not peer.pending


@pytest.mark.asyncio
async def test_bridge_private_origin_binds_exact_target_and_renews_without_origin_header(tmp_path, publication):
    async with live_viewer(tmp_path, publication, "bridge") as (client, app, cfg, site):
        from urllib.parse import urlsplit
        cfg.port = urlsplit(cfg.public_origin).port
        cfg.allow_private_origins = True
        private = f"http://127.0.0.2:{cfg.port}"
        cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
        payload = {"machine_id": "device", "site_id": "robot", "parent_machine_id": "device", "sid": "session"}
        response = await client.post(private + "/api/viewers/open", json=payload,
                                     headers={"Origin": private, "Cookie": cookie})
        assert response.status_code == 200
        grant = response.json()
        runner = await client.get(private + grant["runner"])
        assert runner.status_code == 200
        assert private + "/cc-remote-viewer-runner.js" in runner.headers["content-security-policy"]
        # WebKit does not attach Origin to same-origin GETs. The effective
        # target remains authoritative, never an arbitrary Origin fallback.
        assert (await client.get(private + f'/api/viewers/{grant["id"]}', headers={"Cookie": cookie})).status_code == 200
        assert (await client.get(f'/api/viewers/{grant["id"]}')).status_code == 404
        assert (await client.post("/api/viewers/open", json=payload, headers={"Origin": private})).status_code == 403
        assert (await client.get(grant["runner"])).status_code == 404


async def activate_grant(client, cfg, grant):
    response = await client.get(grant["origin"] + "/__cc_viewer/bootstrap")
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Domain" not in response.headers["set-cookie"]
    config = json.loads(re.search(r"const config = (.*);", response.text)[1])
    activation = await client.post(f'/api/viewers/{grant["id"]}/{config["challenge"]}',
                                   headers={"Origin": cfg.public_origin})
    assert activation.status_code == 200
    return config


@pytest.mark.asyncio
async def test_real_resource_channel_multifile_range_cache_and_close(tmp_path, publication):
    async with live_viewer(tmp_path, publication) as (client, app, cfg, site):
        catalog = await client.get("/api/viewers")
        assert catalog.json()["sites"][0]["machine_id"] == "device"
        assert "root" not in catalog.text
        grant = await create_grant(client, cfg)
        assert "token" not in json.dumps(grant)
        # Knowing a public grant identifier is not a resource credential.
        assert (await client.get(grant["origin"] + site.entry)).status_code == 401
        await activate_grant(client, cfg, grant)
        html = await client.get(grant["origin"] + site.entry)
        assert html.status_code == 200 and 'src="./app.js"' in html.text
        assert "frame-ancestors " + cfg.public_origin in html.headers["content-security-policy"]
        assert "connect-src 'self'" in html.headers["content-security-policy"]
        script = await client.get(grant["origin"] + "/viewer/app.js")
        assert script.status_code == 200 and b"fetch" in script.content
        mesh = await client.get(grant["origin"] + "/meshes/part.stl?v=2")
        assert mesh.content == (Path(site.root) / "meshes/part.stl").read_bytes()
        assert len(mesh.content) > CHUNK_SIZE * 3
        assert mesh.headers["cache-control"] == "private, no-cache"
        cached = await client.get(grant["origin"] + "/meshes/part.stl",
                                   headers={"If-None-Match": mesh.headers["etag"]})
        assert cached.status_code == 304 and not cached.content
        part = await client.get(grant["origin"] + "/meshes/part.stl", headers={"Range": "bytes=2-7"})
        assert part.status_code == 206 and part.content == bytes(range(2, 8))
        head = await client.head(grant["origin"] + "/meshes/part.stl")
        assert head.status_code == 200 and not head.content
        assert head.headers["content-length"] == str(len(mesh.content))
        assert (await client.get(grant["origin"] + "/private.json")).status_code == 403
        assert (await client.post(grant["origin"] + site.entry)).status_code == 405
        assert (await client.get(grant["origin"] + "/api/session")).status_code == 403
        assert not app.state.hub.machine_ids  # No conversation/engine channel needed.
        assert not app.state.viewers.peers["device"].pending
        closed = await client.delete(f'/api/viewers/{grant["id"]}', headers={"Origin": cfg.public_origin})
        assert closed.status_code == 200
        assert (await client.get(grant["origin"] + site.entry)).status_code == 410
        assert (Path(site.root) / "viewer/index.html").exists()  # Closing never deletes source.


@pytest.mark.asyncio
async def test_grants_cannot_cross_login_device_or_challenge(tmp_path, publication):
    async with live_viewer(tmp_path, publication) as (client, app, cfg, site):
        grant = await create_grant(client, cfg)
        bad = await client.post("/api/viewers/open", headers={"Origin": "https://evil.example"}, json={})
        assert bad.status_code == 403
        # A second login, even for the same subject, cannot activate the first frame.
        response = await client.get(grant["origin"] + "/__cc_viewer/bootstrap")
        config = json.loads(re.search(r"const config = (.*);", response.text)[1])
        await client.post("/api/login", json={"password": cfg.login_password})
        assert (await client.post(f'/api/viewers/{grant["id"]}/{config["challenge"]}',
                                  headers={"Origin": cfg.public_origin})).status_code == 404
        own = await create_grant(client, cfg)
        assert (await client.post(f'/api/viewers/{own["id"]}/{config["challenge"]}',
                                  headers={"Origin": cfg.public_origin})).status_code == 409
        await activate_grant(client, cfg, own)
        record = app.state.viewers.grants[own["id"]]
        original_allowed = app.state.viewers.allow_machine
        async def denied(claims, machine):
            return False
        app.state.viewers.allow_machine = denied
        assert (await client.get(own["origin"] + site.entry)).status_code == 403
        app.state.viewers.allow_machine = original_allowed
        record.expires = time.time() - 1
        assert (await client.get(own["origin"] + site.entry)).status_code == 410


@pytest.mark.asyncio
async def test_logout_and_removal_revoke_previews(tmp_path, publication):
    async with live_viewer(tmp_path, publication) as (client, app, cfg, site):
        grant = await create_grant(client, cfg)
        await activate_grant(client, cfg, grant)
        # Match the CLI's atomic registry update. Truncating a file that the
        # catalog worker is reading can intentionally disconnect the peer.
        with edit_sites(publication[1]) as sites:
            sites.clear()
        # Before catalog propagation the Wrapper denies the read; afterwards
        # the Relay expires the old publication. Neither may serve bytes.
        assert (await client.get(grant["origin"] + site.entry)).status_code in {403, 410}
        async with asyncio.timeout(5):
            while app.state.viewers.peers["device"].publication(site.id):
                await asyncio.sleep(0.01)
        assert (await client.get(grant["origin"] + site.entry)).status_code == 410
        await client.post("/api/logout", headers={"Origin": cfg.public_origin})
        assert (await client.get(grant["origin"] + site.entry)).status_code == 401


@pytest.mark.asyncio
async def test_capacity_and_pull_backpressure_are_bounded(tmp_path, publication):
    async with live_viewer(tmp_path, publication) as (client, app, cfg, site):
        grant = await create_grant(client, cfg)
        peer = app.state.viewers.peers["device"]
        identity, status, _ = await peer.read(peer.sites[site.id], "/meshes/part.stl", "GET", {})
        assert status == 200
        await asyncio.sleep(0.05)
        assert peer.pending[identity].chunks.empty()  # No unsolicited model bytes.
        await peer.cancel(identity)
        assert not peer.pending
        original = app.state.viewers.grants[grant["id"]]
        app.state.viewers.grants.update({f"cap-{i}": original for i in range(MAX_GRANTS)})
        full = await client.post("/api/viewers/open", headers={"Origin": cfg.public_origin},
                                 json={"machine_id": "device", "site_id": site.id,
                                       "parent_machine_id": "device", "sid": "session"})
        assert full.status_code == 429
        with pytest.raises(ViewerError):
            peer.closed = True
            await peer.read(peer.sites[site.id], site.entry, "GET", {})


@pytest.mark.asyncio
async def test_viewer_parallel_mesh_burst_queues_without_dropping_files(tmp_path, publication):
    async with live_viewer(tmp_path, publication) as (client, app, cfg, site):
        grant = await create_grant(client, cfg)
        await activate_grant(client, cfg, grant)
        responses = await asyncio.gather(*(
            client.get(grant["origin"] + f"/meshes/part.stl?v={i}") for i in range(43)))
        assert all(response.status_code == 200 for response in responses), [
            (response.status_code, response.text[:200]) for response in responses if response.status_code != 200]
        expected = (Path(site.root) / "meshes/part.stl").read_bytes()
        assert all(response.content == expected for response in responses)
        peer = app.state.viewers.peers["device"]
        assert not peer.pending and not peer.waiting_reads
        assert peer.read_slots._value == MAX_REQUESTS


@pytest.mark.asyncio
async def test_cancelled_mesh_queue_releases_only_its_own_slot(tmp_path, publication):
    async with live_viewer(tmp_path, publication) as (_, app, _, site):
        peer = app.state.viewers.peers["device"]
        active = [await peer.read(peer.sites[site.id], site.entry, "GET", {})
                  for _ in range(MAX_REQUESTS)]
        queued = asyncio.create_task(peer.read(peer.sites[site.id], site.entry, "GET", {}))
        while not peer.waiting_reads:
            await asyncio.sleep(0)
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        assert not peer.waiting_reads and peer.read_slots.locked()
        await asyncio.gather(*(peer.cancel(identity) for identity, _, _ in active))
        assert not peer.pending and peer.read_slots._value == MAX_REQUESTS


@pytest.mark.asyncio
async def test_preview_lease_renewal_expiry_and_device_revocation(tmp_path, publication, monkeypatch):
    async with live_viewer(tmp_path, publication) as (client, app, cfg, site):
        grant = await create_grant(client, cfg)
        await activate_grant(client, cfg, grant)
        record = app.state.viewers.grants[grant["id"]]
        record.expires = time.time() + 1
        health = await client.get(f'/api/viewers/{grant["id"]}', headers={"Origin": cfg.public_origin})
        assert health.status_code == 200 and health.json()["expires_at"] > time.time() + 80
        record.maximum_expires = time.time() + 20
        health = await client.get(f'/api/viewers/{grant["id"]}', headers={"Origin": cfg.public_origin})
        assert health.json()["expires_at"] == record.maximum_expires
        record.expires = time.time() - 1
        assert (await client.get(grant["origin"] + site.entry)).status_code == 410
        grant = await create_grant(client, cfg)
        await activate_grant(client, cfg, grant)
        peer = app.state.viewers.peers["device"]
        async def revoke(machine, subject):
            return machine == "device"
        monkeypatch.setattr(app.state.device_store, "revoke", revoke)
        response = await client.delete("/api/devices/device", headers={"Origin": cfg.public_origin})
        assert response.status_code == 200
        assert peer.closed and grant["id"] not in app.state.viewers.grants
        assert (await client.get(grant["origin"] + site.entry)).status_code == 410
