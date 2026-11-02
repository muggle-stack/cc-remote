"""Content-based validators for fixed-name Web application assets."""
from __future__ import annotations

import hashlib
from pathlib import Path

import anyio
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, Response
from starlette.staticfiles import NotModifiedResponse, StaticFiles
from starlette.types import Scope


_SHELL_FILES = frozenset({
    ".", "index.html", "sw.js", "cc-remote-build.json", "cc-remote-viewer-runner.js",
})


def _read_shell(path: str) -> tuple[bytes, str]:
    body = Path(path).read_bytes()
    return body, f'"{hashlib.sha256(body).hexdigest()}"'


class WebStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        if path not in _SHELL_FILES:
            return await super().get_response(path, scope)

        # Keep StaticFiles' method/path/symlink checks, but do not let its
        # mtime-size validator return 304 before we inspect the actual bytes.
        # Reproducible releases give different, equally sized files the same
        # mtime. Neither that ETag nor If-Modified-Since can validate this shell.
        unconditional = {**scope, "headers": [
            (key, value) for key, value in scope["headers"]
            if key.lower() not in {b"if-none-match", b"if-modified-since"}
        ]}
        response = await super().get_response(path, unconditional)
        if not isinstance(response, FileResponse) or response.status_code != 200:
            return response

        try:
            body, etag = await anyio.to_thread.run_sync(_read_shell, response.path)
        except FileNotFoundError:
            raise HTTPException(status_code=404) from None
        except PermissionError:
            raise HTTPException(status_code=401) from None
        headers = {"etag": etag, "cache-control": "no-cache, must-revalidate"}
        condition = Headers(scope=scope).get("if-none-match", "")
        if any(tag.strip().removeprefix("W/") in {etag, "*"}
               for tag in condition.split(",")):
            return NotModifiedResponse(headers)

        # Send precisely the bytes we hashed, even if the file is replaced
        # between lookup and response. HEAD retains the GET representation size.
        headers["content-length"] = str(len(body))
        return Response(
            b"" if scope["method"] == "HEAD" else body,
            media_type=response.media_type, headers=headers,
        )
