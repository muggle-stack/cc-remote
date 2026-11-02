"""Independent, pull-driven binary Viewer channel. Never enters chat replay."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect

from cc_remote.log import logger
from cc_remote.protocol import PROTOCOL_VERSION
from cc_remote.viewer import (
    CHUNK_SIZE, MAX_REQUESTS, PULL_WINDOW, REQUEST_ID_RE, load_sites, open_resource,
    resource_headers,
)
from cc_remote.viewer_pages import locate_pages, verify_page

log = logger("cc_remote.wrapper.viewer")


class ViewerTransport:
    def __init__(self, url: str, token: str, machine_id: str, registry: Path,
                 session_pages=None, home_pages=None, media_previews=None):
        parts = urlsplit(url)
        self.url = urlunsplit((parts.scheme, parts.netloc, "/ws/viewer", "", ""))
        self.token = token
        self.machine_id = machine_id
        self.registry = registry
        self.session_pages = session_pages
        self.home_pages = home_pages
        self.media_previews = media_previews

    def resource_sites(self):
        manual = load_sites(self.registry)
        return {**(self.home_pages.sites() if self.home_pages else {}), **manual}

    async def run(self) -> None:
        backoff = 1
        while True:
            try:
                sites = await asyncio.to_thread(load_sites, self.registry)
                if not sites and self.session_pages is None and self.media_previews is None:
                    await asyncio.sleep(5)
                    continue
                options = {"additional_headers": {"Authorization": f"Bearer {self.token}"},
                           "max_size": 64 * 1024, "max_queue": 4,
                           "compression": None}
                if urlsplit(self.url).hostname in {"localhost", "127.0.0.1", "::1"}:
                    options["proxy"] = None
                async with connect(self.url, **options) as ws:
                    backoff = 1
                    await self.serve(ws, sites)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never log registry contents, resource paths or credentials.
                log.warning("Viewer channel unavailable; retrying", backoff=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def serve(self, ws, initial_sites) -> None:
        send_lock = asyncio.Lock()
        tasks: dict[str, asyncio.Task] = {}
        pulls: dict[str, asyncio.Queue] = {}
        metadata_tasks: dict[str, asyncio.Task] = {}

        async def send(payload):
            async with send_lock:
                await asyncio.wait_for(ws.send(
                    payload if isinstance(payload, bytes) else json.dumps(payload)), 15)

        async def catalog():
            previous = None
            try:
                while True:
                    current = await asyncio.to_thread(load_sites, self.registry)
                    public = [site.public() for site in current.values()]
                    if public != previous:
                        await send({"type": "catalog", "sites": public})
                        previous = public
                    await asyncio.sleep(3)
            except Exception:
                await ws.close()
                raise

        async def metadata(message):
            result = None
            try:
                action, payload = message.get("action"), message.get("payload")
                if not isinstance(payload, dict):
                    raise ValueError("invalid metadata")
                if action == "locate":
                    paths = payload.get("paths")
                    if (set(payload) != {"paths"} or not isinstance(paths, list)
                            or len(paths) > 16 or any(not isinstance(p, str) or len(p) > 4096 for p in paths)):
                        raise ValueError("invalid candidates")
                    locate = self.home_pages.locate if self.home_pages else locate_pages
                    result = await asyncio.to_thread(locate, self.registry, paths)
                elif action == "verify":
                    entries = payload.get("entries")
                    if set(payload) != {"entries"} or not isinstance(entries, list) or len(entries) > 32:
                        raise ValueError("invalid entries")
                    def verify():
                        sites = self.resource_sites()
                        values = []
                        for entry in entries:
                            if not isinstance(entry, dict) or set(entry) != {"site_id", "entry"}:
                                raise ValueError("invalid entry")
                            site = sites.get(entry["site_id"])
                            if site is None:
                                continue
                            try:
                                values.append({**verify_page(site, entry["entry"]),
                                               "publication": site.public()})
                            except (OSError, ValueError):
                                continue
                        return values
                    result = await asyncio.to_thread(verify)
                elif action == "session" and self.session_pages is not None:
                    result = await self.session_pages(payload)
                else:
                    raise ValueError("unsupported metadata")
                await send({"type": "metadata", "request_id": message["request_id"], "result": result})
            except asyncio.CancelledError:
                raise
            except Exception:
                await send({"type": "metadata", "request_id": message["request_id"], "error": "page_request_failed"})

        async def request(message, ready):
            request_id = message["request_id"]
            stream = None
            began = False
            try:
                media = message.get("type") == "media_request"
                if media:
                    if self.media_previews is None:
                        raise PermissionError("video unavailable")
                    open_args = (self.media_previews.open, message.get("media_id"), message.get("owner_id"))
                else:
                    sites = await asyncio.to_thread(self.resource_sites)
                    site = sites.get(message.get("site_id"))
                    if site is None or message.get("revision") != site.revision:
                        raise PermissionError("publication changed")
                    open_args = (open_resource, site, message.get("path", ""))
                if message.get("method") not in {"GET", "HEAD"}:
                    raise PermissionError("read only")
                headers = message.get("headers", {})
                if (not isinstance(headers, dict) or len(headers) > 3
                        or any(k not in {"range", "if-none-match", "if-range"}
                               or not isinstance(v, str) or len(v) > 256
                               for k, v in headers.items())):
                    raise ValueError("invalid headers")
                opening = asyncio.create_task(asyncio.to_thread(*open_args))
                try:
                    stream, info, mime = await asyncio.shield(opening)
                except asyncio.CancelledError:
                    # to_thread cannot cancel an OS open; collect and close its
                    # eventual FD rather than leaking one on every closed tab.
                    try:
                        abandoned, _, _ = await opening
                        abandoned.close()
                    except Exception:
                        pass
                    raise
                status, response_headers, start, remaining = resource_headers(info, mime, headers)
                await send({"type": "response", "request_id": request_id,
                            "status": status, "headers": response_headers})
                began = True
                if message["method"] == "HEAD":
                    remaining = 0
                stream.seek(start)
                while True:
                    await asyncio.wait_for(ready.get(), timeout=45)
                    # Removal / configuration change revokes even an ongoing download.
                    if media:
                        await asyncio.to_thread(self.media_previews.validate,
                            message.get("media_id"), message.get("owner_id"), stream)
                    else:
                        current = (await asyncio.to_thread(self.resource_sites)).get(site.id)
                        if current is None or current.revision != site.revision:
                            raise PermissionError("publication revoked")
                    def read_chunk():
                        import os
                        now = os.fstat(stream.fileno())
                        if now.st_size != info.st_size or now.st_mtime_ns != info.st_mtime_ns:
                            raise OSError("resource changed during transfer")
                        return stream.read(min(CHUNK_SIZE, remaining))
                    chunk = await asyncio.to_thread(read_chunk)
                    if remaining and not chunk:
                        raise OSError("resource truncated")
                    await send(bytes.fromhex(request_id) + chunk)
                    remaining -= len(chunk)
                    if not chunk:
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status = (404 if isinstance(exc, FileNotFoundError) else
                          403 if isinstance(exc, PermissionError) else 502)
                await send({"type": "failed", "request_id": request_id,
                            "status": status if not began else 502})
            finally:
                if stream:
                    stream.close()

        await send({"type": "hello", "v": PROTOCOL_VERSION,
                    "machine_id": self.machine_id,
                    "session_pages": self.session_pages is not None,
                    "media_previews": self.media_previews is not None,
                    "sites": [site.public() for site in initial_sites.values()]})
        catalog_task = asyncio.create_task(catalog())
        try:
            async for raw in ws:
                if not isinstance(raw, str) or len(raw) > 64 * 1024:
                    raise ValueError("invalid Viewer command")
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError("invalid Viewer command")
                request_id = message.get("request_id", "")
                if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
                    raise ValueError("invalid Viewer request identity")
                kind = message.get("type")
                if kind == "metadata":
                    metadata_tasks = {key: task for key, task in metadata_tasks.items() if not task.done()}
                    if request_id in metadata_tasks or len(metadata_tasks) >= 4:
                        await send({"type": "metadata", "request_id": request_id, "error": "viewer_busy"})
                        continue
                    task = asyncio.create_task(metadata(message))
                    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                    metadata_tasks[request_id] = task
                elif kind in {"request", "media_request"}:
                    if request_id in tasks:
                        raise ValueError("duplicate Viewer request")
                    tasks = {key: task for key, task in tasks.items() if not task.done()}
                    pulls = {key: value for key, value in pulls.items() if key in tasks}
                    if len(tasks) >= MAX_REQUESTS:
                        await send({"type": "failed", "request_id": request_id, "status": 429})
                        continue
                    ready = asyncio.Queue(maxsize=PULL_WINDOW)
                    pulls[request_id] = ready
                    tasks[request_id] = asyncio.create_task(request(message, ready))
                    tasks[request_id].add_done_callback(
                        lambda task: task.exception() if not task.cancelled() else None)
                elif kind == "pull" and request_id in pulls:
                    credits = message.get("credits")
                    if type(credits) is not int or not 1 <= credits <= PULL_WINDOW:
                        raise ValueError("invalid Viewer credit")
                    for _ in range(credits):
                        pulls[request_id].put_nowait(None)
                elif kind == "cancel" and request_id in tasks:
                    task = tasks.pop(request_id)
                    pulls.pop(request_id, None)
                    task.cancel()
                    # The next queued request may immediately reuse this slot.
                    # Finish FD cleanup before counting/admitting it.
                    await asyncio.gather(task, return_exceptions=True)
                elif kind not in {"pull", "cancel"}:
                    raise ValueError("invalid Viewer command")
                if catalog_task.done():
                    catalog_task.result()
        finally:
            for task in metadata_tasks.values():
                task.cancel()
            await asyncio.gather(*metadata_tasks.values(), return_exceptions=True)
            catalog_task.cancel()
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(catalog_task, *tasks.values(), return_exceptions=True)
