"""Explicit, read-only static Viewer publications (no model or HTTP proxy).

Run ``python -m cc_remote.viewer --help`` on the machine owning the files.
The private registry is hot-reloaded by the independent resource transport.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CHUNK_SIZE = 64 * 1024
PULL_WINDOW = 8
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_REQUESTS = 8
MAX_SITES = 32
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
REQUEST_ID_RE = re.compile(r"[a-f0-9]{32}")
SCRIPT_ORIGINS = {"https://esm.sh", "https://cdn.jsdelivr.net", "https://unpkg.com"}
EXTENSIONS = {
    ".html", ".htm", ".js", ".mjs", ".css", ".json", ".wasm",
    ".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".ico", ".avif",
    ".stl", ".glb", ".gltf", ".bin", ".obj", ".mtl",
    ".woff", ".woff2", ".ttf", ".otf", ".txt",
    ".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".flac",
    ".mp4", ".m4v", ".webm", ".ogv",
}
MIME_TYPES = {".js": "text/javascript", ".mjs": "text/javascript",
              ".stl": "model/stl", ".glb": "model/gltf-binary",
              ".gltf": "model/gltf+json", ".wasm": "application/wasm",
              ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
              ".wav": "audio/wav", ".ogg": "audio/ogg", ".oga": "audio/ogg",
              ".opus": "audio/ogg", ".flac": "audio/flac",
              ".mp4": "video/mp4", ".m4v": "video/mp4",
              ".webm": "video/webm", ".ogv": "video/ogg"}


def registry_path() -> Path:
    return Path(os.environ.get(
        "CC_REMOTE_VIEWERS_FILE", str(Path.home() / ".cc-remote" / "viewers.json"),
    )).expanduser()


def clean_path(value: str) -> str:
    """Decode once, reject ambiguous paths instead of normalizing traversal."""
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        raise ValueError("invalid resource path")
    path = unquote(value, errors="strict")
    if (not path.startswith("/") or path.startswith("//")
            or any(c in path for c in "\\%?#")
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or any(part.startswith(".") for part in path.split("/") if part)
            or "//" in path):
        raise ValueError("invalid resource path")
    return path


class ViewerSite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1, max_length=48)
    label: str = Field(min_length=1, max_length=80)
    root: str = Field(min_length=1, max_length=4096)
    root_device: int
    root_inode: int
    entry: str
    paths: list[str] = Field(min_length=1, max_length=32)
    script_origins: list[str] = Field(default_factory=list, max_length=3)
    # Optional exact old LAN URL aliases. They are labels, never fetch targets.
    urls: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not ID_RE.fullmatch(value):
            raise ValueError("site id must contain lowercase letters, digits or hyphens")
        return value

    @field_validator("entry")
    @classmethod
    def valid_entry(cls, value: str) -> str:
        value = clean_path(value)
        if Path(value).suffix.lower() not in {".html", ".htm"}:
            raise ValueError("entry must be an HTML file")
        return value

    @field_validator("paths")
    @classmethod
    def valid_paths(cls, values: list[str]) -> list[str]:
        result = [clean_path(value) for value in values]
        if "/" in result:
            raise ValueError("publish specific resource directories, not the entire root")
        return result

    @field_validator("root")
    @classmethod
    def valid_root(cls, value: str) -> str:
        path = Path(value)
        if (not path.is_absolute() or path == Path("/")
                or path == Path.home() or path in Path.home().parents
                or ".." in path.parts or "\x00" in value):
            raise ValueError("root must be a specific absolute project directory")
        return value

    @field_validator("script_origins")
    @classmethod
    def valid_scripts(cls, values: list[str]) -> list[str]:
        if any(value not in SCRIPT_ORIGINS for value in values):
            raise ValueError("unsupported script origin")
        return sorted(set(values))

    @field_validator("urls")
    @classmethod
    def valid_urls(cls, values: list[str]) -> list[str]:
        for value in values:
            parsed = urlsplit(value)
            if (len(value) > 2048 or parsed.scheme not in {"http", "https"}
                    or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment
                    or any(ord(c) < 32 for c in value)):
                raise ValueError("URL aliases must be http(s) URLs without credentials or query")
        return values

    @model_validator(mode="after")
    def entry_allowed(self):
        if not self.allows(self.entry):
            raise ValueError("entry must be included in the published paths")
        return self

    def allows(self, path: str) -> bool:
        return any(path == scope or (scope.endswith("/") and path.startswith(scope))
                   for scope in self.paths)

    @property
    def revision(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:32]

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "entry": self.entry,
                "revision": self.revision, "script_origins": self.script_origins,
                "urls": self.urls}

    def open_root(self) -> int:
        return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def load_sites(path: Path, *, model=ViewerSite, limit=MAX_SITES,
               max_bytes=64 * 1024) -> dict[str, ViewerSite]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > max_bytes):
            raise ValueError("Viewer registry must be a private, bounded regular file")
        raw = source.read(max_bytes + 1)
    data = json.loads(raw)
    if (not isinstance(data, dict) or set(data) != {"sites"}
            or not isinstance(data["sites"], list) or len(data["sites"]) > limit):
        raise ValueError("invalid Viewer registry")
    sites = [model.model_validate(item) for item in data["sites"]]
    if len({site.id for site in sites}) != len(sites):
        raise ValueError("duplicate Viewer id")
    return {site.id: site for site in sites}


@contextmanager
def edit_sites(path: Path, *, model=ViewerSite, limit=MAX_SITES, max_bytes=64 * 1024):
    """Serialize local registrations; readers only ever see a complete snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(lock_fd, "r+b") as lock:
        info = os.fstat(lock.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError("invalid Viewer registry lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        sites = load_sites(path, model=model, limit=limit, max_bytes=max_bytes)
        before = {key: site.model_dump() for key, site in sites.items()}
        yield sites
        if len(sites) > limit:
            raise ValueError("too many publications")
        if before == {key: site.model_dump() for key, site in sites.items()}:
            return
        encoded = json.dumps({"sites": [site.model_dump() for site in sites.values()]},
                             ensure_ascii=False, indent=2).encode()
        if len(encoded) > max_bytes:
            raise ValueError("Viewer registry full")
        fd, temporary = tempfile.mkstemp(prefix=".viewers-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def open_resource(site: ViewerSite, raw_path: str):
    path = clean_path(raw_path)
    if not site.allows(path) or Path(path).suffix.lower() not in EXTENSIONS:
        raise PermissionError("resource is outside this publication")
    # Anchor every component to a verified directory FD; never follow symlinks,
    # including a swapped root, or reopen by path after validating it.
    fd = site.open_root()
    try:
        root = os.fstat(fd)
        if (root.st_dev != site.root_device or root.st_ino != site.root_inode
                or root.st_uid != os.getuid()):
            raise PermissionError("publication root changed")
        parts = path.lstrip("/").split("/")
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_size > MAX_FILE_BYTES or info.st_nlink != 1):
            raise PermissionError("resource is not a bounded, privately owned regular file")
        stream = os.fdopen(fd, "rb")
        fd = -1
        suffix = Path(path).suffix.lower()
        mime = MIME_TYPES.get(suffix) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        return stream, info, mime
    finally:
        if fd >= 0:
            os.close(fd)


def resource_headers(info: os.stat_result, mime: str, headers: dict[str, str]):
    etag = f'W/"{info.st_dev:x}-{info.st_ino:x}-{info.st_size:x}-{info.st_mtime_ns:x}"'
    result = {"content-type": mime, "etag": etag, "accept-ranges": "bytes"}
    if headers.get("if-none-match") in {etag, "*"}:
        return 304, result, 0, 0
    start, end = 0, info.st_size
    status = 200
    requested = headers.get("range")
    # Weak stat validators cannot safely satisfy If-Range.
    if requested and not headers.get("if-range"):
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
        if not match or not any(match.groups()) or not info.st_size:
            return 416, {"content-range": f"bytes */{info.st_size}"}, 0, 0
        left, right = match.groups()
        if left:
            start = int(left)
            end = min(info.st_size, int(right) + 1) if right else info.st_size
        else:
            start = max(0, info.st_size - int(right))
        if start >= end:
            return 416, {"content-range": f"bytes */{info.st_size}"}, 0, 0
        status = 206
        result["content-range"] = f"bytes {start}-{end - 1}/{info.st_size}"
    result["content-length"] = str(end - start)
    return status, result, start, end - start


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=registry_path())
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("register", help="publish a specific static Viewer")
    add.add_argument("id")
    add.add_argument("--label", required=True)
    add.add_argument("--root", type=Path, required=True)
    add.add_argument("--entry", required=True)
    add.add_argument("--path", action="append", required=True, dest="paths")
    add.add_argument("--script-origin", action="append", default=[])
    add.add_argument("--url", action="append", default=[])
    remove = sub.add_parser("remove", help="revoke a publication without deleting its files")
    remove.add_argument("id")
    sub.add_parser("list")
    args = parser.parse_args()
    if args.command == "list":
        sites = load_sites(args.registry)
        print(json.dumps([site.public() for site in sites.values()], ensure_ascii=False, indent=2))
        return
    if args.command == "register":
        root = args.root.resolve(strict=True)
        info = root.stat()
        site = ViewerSite(id=args.id, label=args.label, root=str(root),
                          root_device=info.st_dev, root_inode=info.st_ino,
                          entry=args.entry, paths=args.paths,
                          script_origins=args.script_origin, urls=args.url)
        stream, _, _ = open_resource(site, site.entry)
        stream.close()
        with edit_sites(args.registry) as sites:
            sites[site.id] = site
    else:
        with edit_sites(args.registry) as sites:
            sites.pop(args.id, None)
    print(f"Viewer {args.command} complete; Wrapper reloads the registry automatically.")


if __name__ == "__main__":
    main()
