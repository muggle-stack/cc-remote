"""Private session/page associations, separate from resource publications.

An association never authorizes a file. Every discovery/open must still pass
the publication's FD-based resource checks on the device owning that file.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cc_remote.viewer import ViewerSite, clean_path, load_sites, open_resource

MAX_PAGES = 32
MAX_SCOPES = 512
MAX_STORE_BYTES = 4 * 1024 * 1024
_PROFILE_REVISIONS = "_profile_revisions"


class PageScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sid: str = Field(min_length=1, max_length=256)
    engine: Literal["claude", "codex", "dsh", "antigravity"]
    space: Literal["code", "work"]

    @field_validator("sid")
    @classmethod
    def clean_sid(cls, value):
        if any(ord(c) < 32 for c in value):
            raise ValueError("invalid session")
        return value

    @property
    def key(self):
        return json.dumps([self.engine, self.space, self.sid], separators=(",", ":"))


class PageRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    machine_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$")
    site_id: str = Field(min_length=1, max_length=48, pattern=r"^[a-z0-9][a-z0-9-]*$")
    entry: str = Field(max_length=4096)
    label: str = Field(min_length=1, max_length=80)
    references: list[str] = Field(default_factory=list, max_length=16)
    turn_ids: list[str] = Field(default_factory=list, max_length=16)
    removed: bool = False

    @field_validator("entry")
    @classmethod
    def html_entry(cls, value):
        return ViewerSite.valid_entry(value)

    @field_validator("references", "turn_ids")
    @classmethod
    def bounded_strings(cls, values):
        if any(not 1 <= len(v) <= 4096 or any(ord(c) < 32 for c in v) for v in values):
            raise ValueError("invalid page reference")
        return list(dict.fromkeys(values))

    @property
    def id(self):
        value = json.dumps([self.machine_id, self.site_id, self.entry])
        return hashlib.sha256(value.encode()).hexdigest()[:32]

    def public(self):
        return {**self.model_dump(exclude={"removed"}), "id": self.id}


def locate_pages(registry: Path, paths: list[str], *, include_ambiguous=False) -> list[dict]:
    """Resolve exact absolute file references, not URLs or directory scans.

    Do not realpath the supplied path: following a symlink before open_resource
    would bypass its deliberate no-follow rule. Ambiguous publications are not
    guessed, even when both happen to live on this device.
    """
    sites = load_sites(registry)
    result = []
    for raw in paths:
        if (not isinstance(raw, str) or len(raw) > 4096 or not raw.startswith("/")
                or raw.startswith("//") or Path(raw).suffix.lower() not in {".html", ".htm"}
                or any(p in {"..", "."} for p in raw.split("/"))):
            continue
        matches = []
        for site in sites.values():
            try:
                relative = Path(raw).relative_to(site.root).as_posix()
                entry = clean_path("/" + relative)
                matches.append(verify_page(site, entry))
            except (OSError, ValueError):
                continue
        if len(matches) == 1 or include_ambiguous:
            result.extend({**match, "reference": raw} for match in matches)
    return result


def verify_page(site: ViewerSite, entry: str) -> dict:
    entry = ViewerSite.valid_entry(entry)
    stream, info, _ = open_resource(site, entry)
    stream.close()
    fingerprint = f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"
    return {"site_id": site.id, "entry": entry,
            "label": site.label if entry == site.entry else Path(entry).stem[:80],
            "revision": site.revision,
            "content_revision": hashlib.sha256(fingerprint.encode()).hexdigest()[:32]}


class ViewerPageStore:
    def __init__(self, path: Path):
        self.path = path

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1
                    or info.st_size > MAX_STORE_BYTES):
                raise ValueError("invalid page store")
            data = json.loads(stream.read(MAX_STORE_BYTES + 1))
        if (not isinstance(data, dict)
                or len(data) - (_PROFILE_REVISIONS in data) > MAX_SCOPES):
            raise ValueError("invalid page store")
        for key, rows in data.items():
            if key == _PROFILE_REVISIONS:
                if (not isinstance(rows, dict) or set(rows) - {"claude", "codex"}
                        or any(type(v) is not int or v < 0 for v in rows.values())):
                    raise ValueError("invalid page profile revisions")
                continue
            engine, space, sid = json.loads(key)
            if PageScope(engine=engine, space=space, sid=sid).key != key:
                raise ValueError("invalid scope")
            if not isinstance(rows, list) or len(rows) > MAX_PAGES:
                raise ValueError("invalid pages")
            data[key] = [PageRef.model_validate(row).model_dump() for row in rows]
        return data

    @contextmanager
    def _edit(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, "rb") as lock:
            info = os.fstat(lock.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ValueError("invalid page store lock")
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self._read()
            before = json.dumps(data, ensure_ascii=False)
            yield data
            encoded = json.dumps(data, ensure_ascii=False).encode()
            if (len(data) - (_PROFILE_REVISIONS in data) > MAX_SCOPES
                    or len(encoded) > MAX_STORE_BYTES):
                raise ValueError("page store full")
            if encoded == before.encode():
                return
            fd, temporary = tempfile.mkstemp(prefix=".viewer-pages-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def list(self, scope: PageScope) -> list[dict]:
        return [PageRef.model_validate(row).public()
                for row in self._read().get(scope.key, []) if not row["removed"]]

    def associate(self, scope: PageScope, pages: list[PageRef], *, automatic: bool):
        with self._edit() as data:
            rows = {page.id: page for page in map(PageRef.model_validate, data.get(scope.key, []))}
            for page in pages:
                old = rows.get(page.id)
                if old and old.removed and automatic:
                    continue  # Reading old history must not undo an explicit removal.
                if not old and len(rows) >= MAX_PAGES:
                    raise ValueError("session page list full")
                if old:
                    page = page.model_copy(update={
                        "references": list(dict.fromkeys(old.references + page.references))[-16:],
                        "turn_ids": list(dict.fromkeys(old.turn_ids + page.turn_ids))[-16:],
                        "removed": False,
                    })
                rows[page.id] = page
            if rows:
                data[scope.key] = [p.model_dump() for p in rows.values()]
                if len(json.dumps(data[scope.key], ensure_ascii=False).encode()) > 48 * 1024:
                    raise ValueError("session page metadata full")
        return self.list(scope)

    def remove(self, scope: PageScope, page_id: str):
        with self._edit() as data:
            for row in data.get(scope.key, []):
                if PageRef.model_validate(row).id == page_id:
                    row["removed"] = True
        return self.list(scope)

    @staticmethod
    def _merge_rows(rows: list[dict]) -> list[dict]:
        merged = {}
        for page in map(PageRef.model_validate, rows):
            previous = merged.get(page.id)
            if previous:
                page = page.model_copy(update={
                    "references": list(dict.fromkeys(previous.references + page.references))[-16:],
                    "turn_ids": list(dict.fromkeys(previous.turn_ids + page.turn_ids))[-16:],
                    "removed": previous.removed or page.removed,
                })
            merged[page.id] = page
        if len(merged) > MAX_PAGES:
            raise ValueError("session page list full")
        result = [p.model_dump() for p in merged.values()]
        if len(json.dumps(result, ensure_ascii=False).encode()) > 48 * 1024:
            raise ValueError("session page metadata full")
        return result

    def migrate_profile_sessions(
        self, engine: Literal["claude", "codex", "dsh", "antigravity"], transform: Callable[[str], str],
        *, profile_revision: int,
    ) -> None:
        """Rewrite one engine's scopes atomically, once per topology revision.

        Revisions live in the same transaction as the rows: a crash between
        independent store migrations must not replay an account-ID swap. Old
        stores without this private metadata remain readable.
        """
        if (engine not in {"claude", "codex"}
                or type(profile_revision) is not int or profile_revision < 1):
            raise ValueError("invalid page profile migration")
        with self._edit() as data:
            revisions = data.get(_PROFILE_REVISIONS, {})
            if revisions.get(engine, 0) >= profile_revision:
                return
            updated = {}
            for key, rows in data.items():
                if key == _PROFILE_REVISIONS:
                    continue
                row_engine, space, sid = json.loads(key)
                if row_engine != engine:
                    updated[key] = rows
                    continue
                key = PageScope(engine=engine, space=space, sid=transform(sid)).key
                updated[key] = self._merge_rows(updated.get(key, []) + rows)
            data.clear()
            data.update(updated)
            data[_PROFILE_REVISIONS] = {**revisions, engine: profile_revision}

    def rekey(self, engine: str, space: str, old_sid: str, sid: str):
        old, new = (PageScope(engine=engine, space=space, sid=s) for s in (old_sid, sid))
        if old == new:
            return
        with self._edit() as data:
            rows = data.pop(old.key, [])
            if rows:
                data[new.key] = self._merge_rows(rows + data.get(new.key, []))

    def drop(self, engine: str, sid: str):
        with self._edit() as data:
            for space in ("work", "code"):
                data.pop(PageScope(engine=engine, space=space, sid=sid).key, None)
