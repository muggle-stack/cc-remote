"""Fixed-name Web assets must revalidate by bytes across reproducible releases."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from starlette.responses import FileResponse

from cc_remote.config import RelayConfig
from cc_remote.relay.server import create_app


def _app(root, state):
    return create_app(RelayConfig(
        static_dir=str(root),
        login_password="static-shell-test-password",
        session_secret="s" * 48,
        wrapper_token="w" * 48,
        public_origin="https://remote.example",
        device_db_path=str(state / "devices.sqlite3"),
    ))


@pytest.mark.parametrize("url,filename", [
    ("/", "index.html"),
    ("/index.html", "index.html"),
    ("/sw.js", "sw.js"),
    ("/cc-remote-build.json", "cc-remote-build.json"),
    ("/cc-remote-viewer-runner.js", "cc-remote-viewer-runner.js"),
    ("/cc-remote-viewer-zoom.js", "cc-remote-viewer-zoom.js"),
])
def test_same_size_and_mtime_update_invalidates_shell(tmp_path, url, filename):
    root = tmp_path / "web"
    root.mkdir()
    asset = root / filename
    asset.write_bytes(b'{"v":82}')
    os.utime(asset, (0, 0))
    legacy = FileResponse(asset, stat_result=asset.stat()).headers
    with TestClient(_app(root, tmp_path)) as client:
        old = client.get(url)
        assert old.status_code == 200
        # Release archives deliberately normalize every file's timestamp.
        asset.write_bytes(b'{"v":85}')
        os.utime(asset, (0, 0))
        validators = [
            {"If-None-Match": old.headers["etag"]},
            {"If-None-Match": legacy["etag"],
             "If-Modified-Since": legacy["last-modified"]},
            {"If-Modified-Since": legacy["last-modified"]},
        ]
        for headers in validators:
            fresh = client.get(url, headers=headers)
            assert fresh.status_code == 200
            assert fresh.content == b'{"v":85}'
            assert fresh.headers["etag"] != old.headers["etag"]
            assert fresh.headers["etag"] != legacy["etag"]
            assert fresh.headers["cache-control"] == "no-cache, must-revalidate"
            assert "last-modified" not in fresh.headers

        etag = fresh.headers["etag"]
        for condition in (etag, f"W/{etag}", f'"outdated", W/{etag}', "*"):
            for method in ("GET", "HEAD"):
                cached = client.request(method, url, headers={"If-None-Match": condition})
                assert cached.status_code == 304
                assert not cached.content
                assert cached.headers["etag"] == etag
                assert cached.headers["cache-control"] == "no-cache, must-revalidate"

        head = client.head(url, headers={"If-None-Match": old.headers["etag"]})
        assert head.status_code == 200
        assert not head.content
        assert head.headers["content-length"] == str(len(fresh.content))
        assert head.headers["content-type"] == fresh.headers["content-type"]
        assert head.headers["etag"] == etag
        # Retimestamping unchanged content must not invalidate its validator.
        os.utime(asset, (100, 100))
        assert client.get(url, headers={"If-None-Match": etag}).status_code == 304

    with TestClient(_app(root, tmp_path)) as restarted:
        assert restarted.get(url, headers={"If-None-Match": etag}).status_code == 304


def test_shell_cache_preserves_static_routing_and_hashed_assets(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "assets").mkdir()
    (root / "assets" / "index-hash.js").write_text("// immutable")
    outside = tmp_path / "private.txt"
    outside.write_text("must not be served")
    (root / "sw.js").symlink_to(outside)
    with TestClient(_app(root, tmp_path)) as client:
        assert client.get("/cc-remote-viewer-runner.js").status_code == 404
        assert client.post("/sw.js").status_code == 405
        assert client.get("/sw.js").status_code == 404
        assert client.get("/%2e%2e/private.txt").status_code == 404
        asset = client.get("/assets/index-hash.js")
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert client.get("/assets/index-hash.js", headers={
            "If-None-Match": asset.headers["etag"],
        }).status_code == 304
        assert client.get("/healthz").headers["cache-control"] == "no-store"
