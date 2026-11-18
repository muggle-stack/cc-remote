"""Headless browser capture for the workspace inspection tool."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import dataclass
from html import escape
import mimetypes
import os
from pathlib import Path
import shutil
import sys
from urllib.parse import quote, unquote, urlsplit

from cc_remote.wrapper.session_files import read_session_file

VIEWPORT = {"width": 1440, "height": 960}
_RENDER_SECONDS = 25
_ROOT = Path(__file__).resolve().parents[2]
_ORIGIN = "http://cc-remote-artifact.invalid"
_PRESENTATION_PATH = "/presentation-preview.html"
_BUNDLE = "artifact-inspection"
_BUNDLE_URL = _ORIGIN + "/renderer/"
_WORKSPACE_PREFIX = "/workspace/"
_WORKSPACE_URL = _ORIGIN + _WORKSPACE_PREFIX
_DOCUMENT_PATH = _BUNDLE_URL + "artifact-document-preview.html"
_RESOURCE_MIME = {
    ".css": "text/css; charset=utf-8",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".avif": "image/avif",
    ".svg": "image/svg+xml", ".ico": "image/vnd.microsoft.icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
}
_RESOURCE_SUFFIXES = frozenset(_RESOURCE_MIME)
_MAX_RESOURCE_BYTES = 8 * 1024 * 1024
_MAX_RESOURCE_REQUESTS = 64
_STATIC_TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css"}

_HOST = """<!doctype html><meta charset="utf-8">
<style>
html, body, iframe { width: 100%; height: 100%; margin: 0; padding: 0; border: 0; }
iframe { display: block; }
</style>
<script>
window.addEventListener("message", (event) => {
  const frame = document.querySelector("iframe");
  if (event.source === frame?.contentWindow &&
      ["cc-remote-presentation-ready", "cc-remote-artifact-ready"].includes(event.data?.type))
    window.previewReady = true;
});
</script>"""
_SEND_TO_FRAME = """({ data, kind, page, resourceUrl }) => {
  const channel = new MessageChannel();
  channel.port1.onmessage = ({data}) => {
    if (data?.type === "inspection") {
      window.previewResult = data;
      channel.port1.close();
    }
  };
  const bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0)).buffer;
  const type = ({
    svg: "cc-remote-inspect-svg", pptx: "cc-remote-presentation",
  })[kind] ?? "cc-remote-inspect-document";
  document.querySelector("iframe").contentWindow.postMessage({
    type, kind, bytes, inspectPage: page, resourceUrl,
  }, "*", [bytes, channel.port2]);
}"""
_DOCUMENT_CSP = (
    "sandbox allow-scripts; default-src 'none'; "
    f"script-src {_BUNDLE_URL}; style-src 'unsafe-inline' {_BUNDLE_URL}; "
    f"img-src data: blob: {_WORKSPACE_URL}; "
    f"font-src data: blob: {_BUNDLE_URL} {_WORKSPACE_URL}; "
    f"base-uri {_WORKSPACE_URL}; connect-src {_BUNDLE_URL}; "
    "form-action 'none'; object-src 'none'"
)
_STATIC_CSP = (
    "sandbox; default-src 'none'; "
    f"img-src data: blob: {_WORKSPACE_URL}; "
    f"style-src 'unsafe-inline' {_WORKSPACE_URL}; "
    f"font-src data: {_WORKSPACE_URL}; base-uri 'none'; form-action 'none'"
)


@dataclass(frozen=True)
class WorkspaceAssets:
    cwd: str
    relative_path: str
    identity: tuple[int, int]

    @property
    def url(self) -> str:
        return _WORKSPACE_URL + quote(self.relative_path)


def _installed_chromium() -> str | None:
    # The session's project and its relative PATH entries are file inputs.
    search_path = os.pathsep.join(("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/snap/bin"))
    for command in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if path := shutil.which(command, path=search_path):
            return path
    if sys.platform == "darwin":
        for apps in (Path("/Applications"), Path.home() / "Applications"):
            for name in ("Google Chrome", "Chromium"):
                path = apps / f"{name}.app" / "Contents" / "MacOS" / name
                if path.is_file() and os.access(path, os.X_OK):
                    return str(path)
    return None  # Playwright can discover its own Chromium for this user.


@asynccontextmanager
async def _browser_page(
    frame_url: str, *, source: bytes | None = None, bundle: Path | None = None,
    assets: WorkspaceAssets | None = None,
):
    try:
        from playwright.async_api import Error as BrowserError, async_playwright
    except ImportError as exc:
        raise ValueError("The background browser driver is unavailable on this device.") from exc
    try:
        async with asyncio.timeout(_RENDER_SECONDS), async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(
                    headless=True, chromium_sandbox=True, timeout=7_000,
                    executable_path=_installed_chromium(),
                    env={
                        key: os.environ[key]
                        for key in ("HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "XDG_RUNTIME_DIR")
                        if key in os.environ
                    },
                )
            except BrowserError as exc:
                raise ValueError(
                    "Cannot start a sandboxed headless browser on this device. "
                    "Install Chrome or Chromium, or provision Playwright's Chromium for the Wrapper user."
                ) from exc
            try:
                page = await browser.new_page(viewport=VIEWPORT, service_workers="block")
                await _serve_preview(page, frame_url, source=source, bundle=bundle, assets=assets)
                yield page
            finally:
                await browser.close()
    except (TimeoutError, BrowserError) as exc:
        raise ValueError("Background file rendering did not finish.") from exc


async def _serve_preview(page, frame_url: str, *, source, bundle, assets):
    # Static HTML runs in a scriptless sandbox. The document renderer can
    # execute only bundled scripts in its own opaque frame.
    sandbox = "" if source is not None else "allow-scripts"
    host = _HOST + f'<iframe sandbox="{sandbox}" src="{escape(frame_url, quote=True)}"></iframe>'
    frame_loaded = False
    host_loaded = False
    resource_count = 0
    resource_bytes = 0

    async def route(pending):
        nonlocal resource_count, resource_bytes, frame_loaded, host_loaded
        request = pending.request
        url = request.url
        if request.method != "GET":
            await pending.abort()
        elif url == _ORIGIN + "/" and not host_loaded and request.is_navigation_request():
            host_loaded = True
            await pending.fulfill(body=host, content_type="text/html; charset=utf-8")
        elif url == frame_url and not frame_loaded and request.is_navigation_request():
            frame_loaded = True
            if source is None:
                rendered = await asyncio.to_thread(bundle.read_bytes)
                csp = _DOCUMENT_CSP if frame_url == _DOCUMENT_PATH else None
            else:
                rendered = source
                csp = _STATIC_CSP
            await pending.fulfill(
                body=rendered, content_type="text/html; charset=utf-8",
                headers={"Content-Security-Policy": csp} if csp else {},
            )
        elif request.is_navigation_request():
            await pending.abort()
        elif bundle is not None and frame_url == _DOCUMENT_PATH and url.startswith(_BUNDLE_URL):
            relative = unquote(urlsplit(url).path.removeprefix("/renderer/"))
            target = bundle.parent / relative
            if (not relative or relative.startswith("/") or ".." in Path(relative).parts
                    or not target.is_file() or target.suffix == ".html"):
                await pending.abort()
                return
            await pending.fulfill(
                path=target,
                headers={"Access-Control-Allow-Origin": "*"},
                content_type=_STATIC_TYPES.get(target.suffix)
                or mimetypes.guess_type(target.name)[0] or "application/octet-stream",
            )
        elif (assets is not None and url.startswith(_WORKSPACE_URL)
              and request.resource_type in {"stylesheet", "image", "font"}):
            resource_count += 1
            remaining = _MAX_RESOURCE_BYTES - resource_bytes
            if resource_count > _MAX_RESOURCE_REQUESTS or remaining <= 0:
                await pending.abort()
                return
            name = unquote(urlsplit(url).path.removeprefix(_WORKSPACE_PREFIX))
            try:
                relative, data, _, _ = await asyncio.to_thread(
                    read_session_file, assets.cwd, name, allowed_suffixes=_RESOURCE_SUFFIXES,
                    max_bytes=remaining, allow_truncate=False, workspace_identity=assets.identity,
                )
            except ValueError:
                await pending.abort()
                return
            resource_bytes += len(data)
            if resource_bytes > _MAX_RESOURCE_BYTES:
                await pending.abort()
                return
            await pending.fulfill(
                body=data, headers={"Access-Control-Allow-Origin": "*", "X-Content-Type-Options": "nosniff"},
                content_type=_RESOURCE_MIME[Path(relative).suffix.lower()],
            )
        else:
            await pending.abort()

    await page.route("**/*", route)
    await page.goto(_ORIGIN + "/")


async def render_document(
    data: bytes, kind: str, page_number: int, *, assets: WorkspaceAssets | None = None,
) -> tuple[bytes, dict]:
    entry = (_PRESENTATION_PATH.lstrip("/") if kind in {"pptx", "svg"}
             else f"{_BUNDLE}/artifact-document-preview.html")
    renderer = _ROOT / "share" / "cc-remote" / entry
    if not renderer.is_file():
        renderer = _ROOT / "web" / "dist" / entry
    if not renderer.is_file():
        raise ValueError("The bundled file renderer is missing. Build or reinstall the Wrapper bundle.")
    frame_url = _ORIGIN + _PRESENTATION_PATH if kind in {"pptx", "svg"} else _DOCUMENT_PATH
    async with _browser_page(frame_url, bundle=renderer, assets=assets) as page:
        await page.wait_for_function("window.previewReady === true")
        await page.evaluate(_SEND_TO_FRAME, {
            "data": base64.b64encode(data).decode("ascii"),
            "kind": kind, "page": page_number, "resourceUrl": assets.url if assets else None,
        })
        await page.wait_for_function("window.previewResult !== undefined")
        result = await page.evaluate("window.previewResult")
        if result.get("error"):
            raise ValueError(result["error"])
        if kind != "svg" and result.get("pageNumber") != page_number:
            raise ValueError("The renderer returned a different page.")
        frame = page.frame_locator("iframe")
        await frame.locator("body").evaluate("() => document.fonts.ready.then(() => null)")
        target = {"svg": "#inspect-svg", "pptx": "#slide"}.get(kind, "#capture")
        image = await frame.locator(target).screenshot(type="png", animations="disabled")
        return image, result


async def render_static_page(
    data: bytes, page_number: int, *, assets: WorkspaceAssets,
) -> tuple[bytes, int]:
    async with _browser_page(assets.url, source=data, assets=assets) as page:
        body = page.frame_locator("iframe").locator("body")
        count = await body.evaluate("""async (_, pageNumber) => {
          await document.fonts.ready;
          await Promise.allSettled([...document.images].map(image => image.decode()));
          const count = Math.max(1, Math.ceil(document.scrollingElement.scrollHeight / innerHeight));
          if (pageNumber <= count) window.scrollTo(0, (pageNumber - 1) * innerHeight);
          return count;
        }""", page_number)
        if page_number > count:
            raise ValueError(f"Page {page_number} is out of range. This preview has {count} page(s).")
        return await page.screenshot(type="png", animations="disabled"), count


async def render_table(data: bytes) -> bytes:
    async with _browser_page(_WORKSPACE_URL + "table.html", source=data) as page:
        return await page.frame_locator("iframe").locator("#capture").screenshot(type="png")
