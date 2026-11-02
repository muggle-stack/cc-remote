"""Native video file permissions and HTTP ranges across the real binary channel."""
import asyncio
import base64
from functools import partial
from http.cookies import SimpleCookie
from pathlib import Path

import httpx
import pytest

from cc_remote.relay.auth import session_token_claims
from cc_remote.relay.server import _btw_owner_id
from cc_remote.relay.viewer import main_cookie_name
from cc_remote.protocol import (
    FilePreview, GetFilePreview, GetSessionArtifactPreview, deserialize, is_downstream, serialize,
)
from cc_remote.viewer import MAX_FILE_BYTES, MAX_REQUESTS
from cc_remote.wrapper.machine import WrapperMachine
from cc_remote.wrapper.media_preview import MEDIA_TTL, MAX_OWNER_PREVIEWS, MediaPreviews
from tests.test_viewer import live_viewer
from tests import test_viewer
from tests.test_multisession import _mk_ctx, _mk_machine

publication = test_viewer.publication
isolated_login_limiter = test_viewer.isolated_login_limiter


def video_file(root, name="sample.mp4", size=1024 * 1024):
    path = root / name
    with path.open("wb") as stream:
        stream.write(b"\0\0\0\x18ftypisom\0\0\0\0")
        stream.truncate(size)
    return path


def open_video(root, path):
    return WrapperMachine._open_session_file(str(root), str(path),
        allowed_suffixes=frozenset({".mp4", ".m4v"}), max_bytes=MAX_FILE_BYTES,
        allow_truncate=False)


def register(store, root, path, owner="owner-a"):
    return store.create(owner, "claude", "sid", partial(open_video, root, path))["media_id"]


def test_video_descriptor_owner_expiry_file_identity_and_deletion(tmp_path):
    now = [0]
    store = MediaPreviews(clock=lambda: now[0])
    path = video_file(tmp_path)
    key = register(store, tmp_path, path)
    with pytest.raises(FileNotFoundError):
        store.open(key, "owner-b")
    with store.open(key, "owner-a")[0] as stream:
        store.validate(key, "owner-a", stream)
        with path.open("r+b") as writer:
            writer.seek(20)
            writer.write(b"changed")
        with pytest.raises(PermissionError):
            store.validate(key, "owner-a", stream)
    with pytest.raises(PermissionError):
        store.open(key, "owner-a")
    key = register(store, tmp_path, path)
    with store.open(key, "owner-a")[0] as stream:
        store.drop("claude", "sid")
        with pytest.raises(FileNotFoundError):
            store.validate(key, "owner-a", stream)
    key = register(store, tmp_path, path)
    now[0] += MEDIA_TTL
    with pytest.raises(FileNotFoundError):
        store.open(key, "owner-a")


def test_video_does_not_expand_file_permissions_or_read_whole_file(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = MediaPreviews()
    outside = video_file(tmp_path)
    with pytest.raises(ValueError):
        register(store, root, outside)
    (root / "escape.mp4").symlink_to(outside)
    with pytest.raises(ValueError):
        register(store, root, root / "escape.mp4")
    path = video_file(root, size=MAX_FILE_BYTES + 1)
    with pytest.raises(ValueError, match="MiB"):
        register(store, root, path)
    path.write_bytes(b"this is not a movie")
    with pytest.raises(ValueError, match="MP4"):
        register(store, root, path)
    video_file(root, size=MAX_FILE_BYTES)
    # Registration reads just the header even for a sparse 256 MiB file.
    key = register(store, root, path)
    with store.open(key, "owner-a")[0] as stream:
        assert stream.tell() == 0
    path.rename(root / "old.mp4")
    path.symlink_to(outside)
    with pytest.raises(ValueError):
        store.open(key, "owner-a")


def test_video_descriptors_are_bounded_per_owner(tmp_path):
    store = MediaPreviews()
    path = video_file(tmp_path)
    first = register(store, tmp_path, path)
    for _ in range(MAX_OWNER_PREVIEWS):
        register(store, tmp_path, path)
    assert len(store.entries) == MAX_OWNER_PREVIEWS
    with pytest.raises(FileNotFoundError):
        store.open(first, "owner-a")
    with pytest.raises(ValueError):
        register(store, tmp_path, path, owner=None)


@pytest.mark.asyncio
async def test_video_command_returns_private_metadata_and_revokes_external_reads(tmp_path):
    machine, transport = _mk_machine()
    ctx = _mk_ctx("session-a", session_id="session-a")
    root = tmp_path / "project"
    root.mkdir()
    ctx.cwd = str(root)
    ctx.space = "code"
    machine.sessions[ctx.key] = ctx
    path = video_file(tmp_path, size=17 * 1024 * 1024)
    response = await machine._handle_get_file_preview(GetFilePreview(
        sid=ctx.key, client_id="browser", owner_id="owner-a", request_id="video-request", path=str(path)))
    assert isinstance(response, FilePreview) and response.error is None
    assert response.to == "browser" and response.sid == ctx.key
    assert response.format == "video" and response.media_type == "video/mp4"
    assert response.size == 17 * 1024 * 1024
    assert response.data is None and not response.content and not response.writable
    assert response.media_id and not is_downstream(response)
    assert deserialize(serialize(response)) == response
    assert transport.sent[-1] == response
    with machine.media_previews.open(response.media_id, "owner-a")[0] as stream:
        machine._preview_capability_store.revoke(ctx.engine, ctx.space, ctx.key, str(path))
        with pytest.raises(PermissionError):
            machine.media_previews.validate(response.media_id, "owner-a", stream)


def test_session_deletion_during_video_open_does_not_leave_descriptor(tmp_path):
    store = MediaPreviews()
    path = video_file(tmp_path)
    def opener():
        stream = open_video(tmp_path, path)
        store.drop("claude", "sid")
        return stream
    with pytest.raises(ValueError, match="会话已改变"):
        store.create("owner-a", "claude", "sid", opener)
    assert not store.entries


@pytest.mark.asyncio
@pytest.mark.parametrize("upload", [False, True])
async def test_video_artifact_library_preserves_owner_for_link_and_upload(tmp_path, upload):
    machine, _ = _mk_machine()
    ctx = _mk_ctx("session", session_id="session")
    ctx.cwd = str(tmp_path)
    machine.sessions[ctx.key] = ctx
    service = machine._artifacts
    path = video_file(tmp_path)
    if upload:
        await service.uploads(ctx, "msg", [{"filename": "sample.mp4",
            "data": base64.b64encode(path.read_bytes()).decode()}])
    else:
        service.store.text(service.context_scope(ctx), "[video](sample.mp4)", ctx.cwd)
    entry = service.store.page(service.context_scope(ctx))[0][0]
    response = await service.preview(GetSessionArtifactPreview(
        sid=ctx.key, engine=ctx.engine, space=ctx.space, artifact_id=entry["id"],
        request_id="video", client_id="browser", owner_id="owner-a"))
    assert response.error is None and response.format == "video" and response.media_id
    assert response.data is None
    with machine.media_previews.open(response.media_id, "owner-a")[0] as stream:
        assert stream.read(8).endswith(b"ftyp")
    await machine._drop_preview_session(ctx.engine, ctx.key)
    with pytest.raises(FileNotFoundError):
        machine.media_previews.open(response.media_id, "owner-a")


def owner_for(client, cfg):
    cookie = SimpleCookie()
    cookie.load(f"session={client.cookies.get(main_cookie_name(cfg))}")
    claims = session_token_claims(cookie["session"].value, cfg.session_secret)
    return _btw_owner_id(claims, cfg.session_secret)


@pytest.mark.asyncio
async def test_video_real_http_ranges_authentication_and_release(tmp_path, publication):
    store = MediaPreviews()
    async with live_viewer(tmp_path, publication, mode="bridge", media_previews=store) as (client, app, cfg, site):
        root = Path(site.root)
        path = video_file(root)
        key = register(store, root, path, owner_for(client, cfg))
        url = f"/api/media/device/{key}"
        async with httpx.AsyncClient(base_url=cfg.public_origin, cookies=client.cookies) as http:
            head = await http.head(url)
            assert head.status_code == 200 and not head.content
            assert head.headers["content-length"] == str(path.stat().st_size)
            assert head.headers["accept-ranges"] == "bytes"
            assert head.headers["cache-control"] == "private, no-store"
            assert head.headers["content-type"] == "video/mp4"
            for requested, expected in [("bytes=0-15", (0, 16)), ("bytes=-20", (path.stat().st_size - 20, 20)),
                                        ("bytes=1048570-", (1048570, 6))]:
                response = await http.get(url, headers={"Range": requested})
                start, size = expected
                assert response.status_code == 206
                assert len(response.content) == size
                with path.open("rb") as stream:
                    stream.seek(start)
                    assert response.content == stream.read(size)
            invalid = await http.get(url, headers={"Range": "bytes=999999999-"})
            assert invalid.status_code == 416 and not invalid.content
            assert invalid.headers["content-range"] == f"bytes */{path.stat().st_size}"
            cached = await http.get(url, headers={"If-None-Match": head.headers["etag"]})
            assert cached.status_code == 304
            assert (await http.get(url, headers={"Origin": "https://attacker.invalid"})).status_code == 403
            assert (await http.get(url, headers={"Sec-Fetch-Site": "same-site"})).status_code == 403
            assert (await http.get(f"/api/media/other/{key}")).status_code != 200
            foreign = register(store, root, path, "another-owner")
            assert (await http.get(f"/api/media/device/{foreign}")).status_code == 404
            no_cookie = await client.get(url, headers={"Cookie": ""})
            assert no_cookie.status_code == 401
            peer = app.state.viewers.peers["device"]
            assert not peer.pending and peer.read_slots._value == MAX_REQUESTS
            # Browser seek/close cancels an outstanding source reader and frees its slot.
            large = video_file(root, "large.mp4", size=64 * 1024 * 1024)
            large_key = register(store, root, large, owner_for(client, cfg))
            async with http.stream("GET", f"/api/media/device/{large_key}") as response:
                assert response.status_code == 200
                async for chunk in response.aiter_bytes():
                    assert chunk
                    break
            async with asyncio.timeout(5):
                while peer.pending:
                    await asyncio.sleep(0.01)
            assert peer.read_slots._value == MAX_REQUESTS
            store.drop("claude", "sid")
            assert (await http.get(url)).status_code == 404
            await http.post("/api/logout")
            assert (await http.get(url)).status_code == 401


@pytest.mark.asyncio
async def test_video_rechecks_device_access_for_http(tmp_path, publication):
    store = MediaPreviews()
    async with live_viewer(tmp_path, publication, mode="bridge", media_previews=store) as (client, app, cfg, site):
        key = register(store, Path(site.root), video_file(Path(site.root)), owner_for(client, cfg))
        async def deny(*args):
            return False
        app.state.viewers.allow_machine = deny
        response = await client.get(f"/api/media/device/{key}")
        assert response.status_code == 403
        assert not app.state.viewers.peers["device"].pending
