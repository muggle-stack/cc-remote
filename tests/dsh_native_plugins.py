"""Explicit native plugin update probe using a loopback-only npm fixture."""
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import tarfile
from threading import Thread
from uuid import uuid4

from cc_remote.protocol import ReadDsh, SetDshControl


async def exercise(machine, runtime, sid):
    package = "cc-remote-offline-bundle"
    tarballs, manifests = {}, {}
    for version in ("1.0.0", "1.1.0", "1.2.0"):
        manifest = {"name": package, "version": version, "type": "module",
                    "dsh": {"bundle": {"patch": "./bundle.yml"}}}
        manifests[version] = manifest
        content = io.BytesIO()
        with tarfile.open(fileobj=content, mode="w:gz") as archive:
            files = {"package.json": json.dumps(manifest), "bundle.yml": "[]\n", "README.md": "# Offline plugin\n\n- Native rich detail\n\n```sh\necho fixture\n```\n"}
            # Installation failure after the old version was removed exercises
            # native rollback without executing package scripts or a model.
            if version == "1.2.0":
                files.pop("bundle.yml")
            for name, text in files.items():
                raw = text.encode()
                info = tarfile.TarInfo("package/" + name)
                info.size, info.mode = len(raw), 0o644
                archive.addfile(info, io.BytesIO(raw))
        tarballs[version] = content.getvalue()

    class Registry(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/index.json":
                body = json.dumps({"format": 1, "title": "Offline market", "plugins": [{
                    "name": package, "title": "Offline plugin", "description": "Loopback acceptance fixture",
                    "versions": [{"version": "1.0.0", "publishTimestamp": 1, "source": {
                        "type": "npm", "registry": f"http://127.0.0.1:{self.server.server_port}"}, "engines": {}}]}]}).encode()
                kind = "application/json"
            elif self.path.split("?")[0] == "/" + package:
                body = json.dumps({"name": package, "dist-tags": {"latest": "1.2.0"}, "versions": {
                    version: {**manifest, "dist": {"tarball": f"http://127.0.0.1:{self.server.server_port}/{version}.tgz",
                        "shasum": hashlib.sha1(tarballs[version]).hexdigest()}}
                    for version, manifest in manifests.items()}}).encode()
                kind = "application/json"
            elif self.path.removeprefix("/").removesuffix(".tgz") in tarballs:
                body = tarballs[self.path.removeprefix("/").removesuffix(".tgz")]
                kind = "application/octet-stream"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Registry)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"

    async def manage(body):
        return await machine._handle(SetDshControl(sid=sid, kind="manage", value=json.dumps(body),
            cmd_id=uuid4().hex, client_id="offline-plugin-probe"))

    try:
        market = await manage({"action": "bundle_enable", "name": "@deepseek-ai/dsh-experimental-plugin-market-bundle"})
        assert market.status == "success", market
        async def read(query):
            page = await machine._handle(ReadDsh(sid=sid, kind="bundles", query=json.dumps(query), cmd_id=uuid4().hex, client_id="offline-plugin-probe"))
            assert not page.error, page
            return page.items
        sources = await read({"sources": True})
        added = await manage({"action": "market_source_add", "url": origin + "/index.json", "revision": sources[0].data["revision"]})
        assert added.status == "success", added
        sources = await read({"sources": True})
        source = next(row for row in sources if row.data.get("url") == origin + "/index.json")
        results = await read({"market": True, "source": source.id, "text": package})
        target = next(row for row in results if row.id == package).data["installTarget"]
        assert target and "registry" not in target and "spec" not in target
        installed = await manage({"action": "market_install", **target})
        assert installed.status == "success", installed
        bundles = await runtime.client.rpc("pluginManager/listBundles")
        bundle = next(row for row in bundles if row["name"] == package)
        assert bundle["updatable"] and bundle["version"] == "1.0.0", bundle
        detail = await read({"detail": True, "name": package})
        assert "# Offline plugin" in detail[0].detail and "Native rich detail" in detail[0].detail
        page = await machine._handle(ReadDsh(sid=sid, kind="bundles", query=json.dumps({"versions": True, "name": package}),
            cmd_id=uuid4().hex, client_id="offline-plugin-probe"))
        assert not page.error and {row.id for row in page.items} == {"1.1.0", "1.2.0"}, page
        updated = await manage({"action": "bundle_update", "name": package, "version": "1.1.0"})
        assert updated.status == "success", updated
        assert next(row for row in await runtime.client.rpc("pluginManager/listBundles") if row["name"] == package)["version"] == "1.1.0"
        failed = await manage({"action": "bundle_update", "name": package, "version": "1.2.0"})
        assert failed.status == "error" and "恢复" in failed.text, failed
        assert next(row for row in await runtime.client.rpc("pluginManager/listBundles") if row["name"] == package)["version"] == "1.1.0"
        removed = await manage({"action": "bundle_remove", "name": package})
        assert removed.status == "success", removed
        sources = await read({"sources": True})
        assert (await manage({"action": "market_source_remove", "source": source.id, "revision": sources[0].data["revision"]})).status == "success"
        print("PASS native market source add/search/install/remove, rich detail, version update and failed-install restoration", flush=True)
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)
