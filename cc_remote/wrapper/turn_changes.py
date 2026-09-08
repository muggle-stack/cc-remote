"""Bounded, source-only turn diffs and a durable archive independent of history LRU.

No worktree reads, git index writes or model requests. Unified patches are
composed against symbolic unchanged lines; inconsistent/incomplete evidence is
reported explicitly instead of inventing a before-image from today's file.
"""
from __future__ import annotations

import difflib
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any

FILE_PAGE_SIZE = 64
MAX_FILES = 4096
MAX_DIFF = 2 * 1024 * 1024
MAX_TURN_DIFF = 4 * MAX_DIFF
MAX_LINES = 30_000
MAX_EVENTS = 4096
MAX_ARCHIVE = 1024 * 1024 * 1024
_PAYLOAD_VERSION = 3
MUTATORS = {"write", "edit", "multiedit", "notebookedit", "editfile", "apply_patch", "filechange"}
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def turn_change_event_data(event) -> dict:
    """Archive-only projection. Never use this helper for wire/cache tool cards."""
    return {**event.model_dump(mode="json"),
            **(getattr(event, "_turn_change_source", None) or {})}


def paths_from_input(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return []
    values = list(data.get("file_paths", [])) if isinstance(data.get("file_paths"), list) else []
    values.extend(data.get(key) for key in ("file_path", "path", "notebook_path"))
    changes = data.get("changes")
    if isinstance(changes, dict):
        values.extend(changes)
        changes = list(changes.values())
    for change in changes if isinstance(changes, list) else []:
        if isinstance(change, dict):
            values.extend(change.get(key) for key in ("path", "move_path", "destination_path", "to"))
    return list(dict.fromkeys(os.path.normpath(value) for value in values
                             if isinstance(value, str) and value and len(value) <= 4096
                             and not any(c in value for c in "\x00\r\n")))[:MAX_FILES + 1]


def _header_path(value: str) -> str:
    value = value.split("\t", 1)[0]
    if value.startswith('"'):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValueError("unsupported quoted path") from exc
    return os.path.normpath(value[2:] if value.startswith(("a/", "b/")) else value)


def native_claude_diff(data: Any, path: str | None = None) -> str | None:
    """Use the tool's actual result, not Edit substring or Write assumptions."""
    if not isinstance(data, dict):
        return None
    paths = paths_from_input({"path": data.get("filePath") or data.get("file_path") or path})
    if not paths:
        return None
    path = paths[0]
    structured = data.get("structuredPatch")
    if not structured:
        # Write returns complete before/after strings, but not necessarily
        # structured hunks. Only its explicit native creation/update result
        # proves the before-image (the Write input alone does not).
        content, original = data.get("content"), data.get("originalFile")
        created = data.get("type") == "create" and "originalFile" in data and original is None
        updated = data.get("type") == "update" and isinstance(original, str)
        if not isinstance(content, str) or not (created or updated):
            return None
        if len(content) + len(original or "") > MAX_DIFF:
            return None
        raw = "\n".join(difflib.unified_diff(
            (original or "").splitlines(), content.splitlines(),
            fromfile="/dev/null" if created else path, tofile=path, lineterm="",
        )) + "\n"
        try:
            parse_patches(raw)
        except ValueError:
            return None
        return raw
    if not isinstance(structured, list) or len(structured) > 1024:
        return None
    lines = []
    for hunk in structured:
        if not isinstance(hunk, dict):
            return None
        counts = [hunk.get(key) for key in ("oldStart", "oldLines", "newStart", "newLines")]
        body = hunk.get("lines")
        if any(type(value) is not int or not 0 <= value <= MAX_LINES for value in counts):
            return None
        if (not isinstance(body, list) or len(body) > MAX_LINES
                or any(not isinstance(line, str) or "\n" in line or "\r" in line for line in body)):
            return None
        lines.append(f"@@ -{counts[0]},{counts[1]} +{counts[2]},{counts[3]} @@")
        lines.extend(body)
        if sum(len(line) for line in lines) > MAX_DIFF:
            return None
    old = "/dev/null" if data.get("type") == "create" else path
    raw = f"--- {old}\n+++ {path}\n" + "\n".join(lines) + "\n"
    try:
        parse_patches(raw)
    except ValueError:
        return None
    return raw


def parse_patches(raw: str) -> list[dict]:
    if len(raw) > MAX_DIFF:
        raise ValueError("diff too large")
    lines = raw.splitlines()
    if len(lines) > MAX_LINES:
        raise ValueError("diff has too many lines")
    patches: list[dict] = []
    current = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
            old = _header_path(line[4:])
            new = _header_path(lines[index + 1][4:])
            current = {"old": old, "new": new, "hunks": []}
            patches.append(current)
            index += 2
            continue
        match = _HUNK.match(line)
        if match and current is not None:
            old_start, old_count, new_start, new_count = match.groups()
            old_count, new_count = int(old_count or 1), int(new_count or 1)
            old_lines, new_lines = [], []
            index += 1
            while index < len(lines) and (len(old_lines) < old_count or len(new_lines) < new_count):
                body = lines[index]
                if body.startswith("\\ No newline"):
                    index += 1
                    continue
                if not body or body[0] not in " +-":
                    raise ValueError("incomplete hunk")
                if body[0] in " -":
                    old_lines.append(body[1:])
                if body[0] in " +":
                    new_lines.append(body[1:])
                index += 1
            if len(old_lines) != old_count or len(new_lines) != new_count:
                raise ValueError("inconsistent hunk")
            current["hunks"].append((int(old_start), old_count, int(new_start), new_count, old_lines, new_lines))
            continue
        if line.startswith(("@@", "Binary files", "GIT binary patch", "+", "-")):
            raise ValueError("unsupported patch")
        if line and not line.startswith(("diff --git ", "index ", "new file mode ",
                                        "deleted file mode ", "old mode ", "new mode ",
                                        "similarity index ", "rename from ", "rename to ",
                                        "\\ No newline at end of file")):
            raise ValueError("unrecognized patch body")
        index += 1
    if not patches or any(not patch["hunks"] for patch in patches):
        raise ValueError("no complete patch")
    return patches


class _File:
    def __init__(self, old: str, new: str):
        self.old, self.new = old, new
        self.before: list[str | int] = []
        self.after: list[str | int] = []
        self.created = old == "/dev/null"
        self.deleted = False

    def apply(self, patch: dict) -> None:
        if self.deleted or (patch["old"] == "/dev/null" and self.after):
            raise ValueError("discontinuous file lifecycle")
        if patch["old"] not in {self.new, "/dev/null"}:
            raise ValueError("discontinuous rename")
        offset, last_end = 0, 0
        for old_start, old_count, new_start, new_count, old_lines, new_lines in patch["hunks"]:
            start = old_start - (1 if old_count else 0)
            if start < last_end or start < 0 or start + old_count > MAX_LINES:
                raise ValueError("invalid hunk range")
            last_end = start + old_count
            position = start + offset
            if position != new_start - (1 if new_count else 0):
                raise ValueError("inconsistent new range")
            needed = position + old_count
            if self.created and needed > len(self.after):
                raise ValueError("patch exceeds known created file")
            while len(self.after) < needed:
                token = len(self.before)
                self.before.append(token)
                self.after.append(token)
            if position > len(self.after):
                raise ValueError("invalid insertion")
            for line_index, expected in enumerate(old_lines):
                token = self.after[position + line_index]
                if isinstance(token, int):
                    self.before[token] = expected
                    self.after[position + line_index] = expected
                elif token != expected:
                    raise ValueError("patch does not match preceding result")
            self.after[position:needed] = new_lines
            if len(self.after) > MAX_LINES:
                raise ValueError("file too large")
            offset += new_count - old_count
        self.new = patch["new"]
        self.deleted = self.new == "/dev/null"
        if self.deleted and self.after:
            raise ValueError("incomplete deletion")

    def render(self) -> tuple[str, int, int]:
        groups = difflib.SequenceMatcher(None, self.before, self.after).get_grouped_opcodes(0)
        lines = []
        additions = deletions = 0
        for group in groups:
            a0, a1 = group[0][1], group[-1][2]
            b0, b1 = group[0][3], group[-1][4]
            lines.append(f"@@ -{a0 + bool(a1-a0)},{a1-a0} +{b0 + bool(b1-b0)},{b1-b0} @@")
            for tag, i, j, k, end in group:
                if tag in {"replace", "delete"}:
                    if any(not isinstance(row, str) for row in self.before[i:j]):
                        raise ValueError("unknown removed content")
                    lines.extend("-" + str(row) for row in self.before[i:j])
                    deletions += j-i
                if tag in {"replace", "insert"}:
                    lines.extend("+" + str(row) for row in self.after[k:end])
                    additions += end-k
        if not lines:
            return "", 0, 0
        # One section per canonical path, including deletion and rename.
        path = self.new if self.new != "/dev/null" else self.old
        return (f"diff --git a/{self.old} b/{path}\n--- {self.old}\n+++ {self.new}\n"
                + "\n".join(lines) + "\n", additions, deletions)


def project_turn_changes(events: list[dict], cwd: str | None = None) -> dict:
    """Rebuild a turn from native evidence, never from the current filesystem."""
    tools: dict[str, dict] = {}
    results: dict[str, dict] = {}
    native = None
    native_index = -1
    result_indexes: dict[str, int] = {}
    def canonical_path(path: str) -> str:
        return (os.path.normpath(os.path.join(cwd, path))
                if cwd and os.path.isabs(cwd) and not os.path.isabs(path)
                else path)
    for index, event in enumerate(events):
        kind = event.get("type")
        if kind == "tool_use" and str(event.get("tool", "")).lower() in MUTATORS:
            tools[event["tool_use_id"]] = event
        elif kind == "tool_result":
            results[event["tool_use_id"]] = event
            result_indexes[event["tool_use_id"]] = index
        elif kind == "turn_diff":
            native = event
            native_index = index
    files: dict[str, dict] = {}
    models: dict[str, _File] = {}
    invalid: set[str] = set()
    operations = []
    overflow = False
    for key, tool in tools.items():
        result = results.get(key)
        if result and result.get("is_error"):
            continue
        source_files = result.get("_file_diffs") if result else None
        paths = [canonical_path(path) for path in paths_from_input(tool.get("input"))]
        if isinstance(source_files, list):
            paths = list(dict.fromkeys([*paths, *(
                canonical_path(path) for entry in source_files
                for path in paths_from_input(entry)
            )]))
        for path in paths:
            if path not in files and len(files) >= MAX_FILES:
                overflow = True
                continue
            files.setdefault(path, {"path": path, "state": "unavailable"})
        if isinstance(source_files, list):
            # Wrapper-private native evidence is captured before the bounded
            # tool card is serialized. File-list pagination must not inherit
            # the card's 64-entry / aggregate-text clipping.
            overflow = overflow or bool(result.get("_files_truncated"))
            operations.extend((entry.get("diff"), [canonical_path(path) for path
                               in paths_from_input(entry)], False)
                              for entry in source_files)
            continue
        raw = None
        # Older Codex records combined output and diff clipping in `truncated`.
        # An explicit False on the newer field proves only the text was clipped;
        # do not discard a complete native Claude/Codex patch in that case.
        diff_truncated = bool(result and (
            result.get("truncated") if result.get("diff_truncated") is None
            else result["diff_truncated"]))
        overflow = overflow or diff_truncated
        if result and result.get("diff") and not diff_truncated:
            # Legacy Claude payload-derived fragments have synthetic line 1
            # and Write incorrectly assumes creation. Keep those out of the
            # cumulative archive unless native metadata proved the patch.
            if (str(tool.get("tool", "")).lower() not in {"edit", "write", "multiedit"}
                    or result.get("diff_source") == "native"):
                raw = result["diff"]
        operations.append((raw, paths, result is None))
    # A native turn snapshot is authoritative only up to its own source order.
    # Never apply an older snapshot over a later mutation or a pending edit.
    native_complete = (native is not None and not native.get("truncated")
                       and all(key in results and result_indexes[key] <= native_index
                               for key in tools))
    if native_complete:
        operations = [(native.get("diff", ""), list(files), False)]
    used = 0
    def invalidate(path: str, *, pending: bool = False) -> None:
        if path not in files:
            return
        files[path] = {"path": path, "state": "pending" if pending else "unavailable"}
        models.pop(path, None)
        invalid.add(path)

    for raw, expected_paths, pending in operations:
        if raw is None:
            for path in expected_paths:
                invalidate(path, pending=pending)
            continue
        try:
            parsed = parse_patches(raw) if raw else []
            covered: set[str] = set()
            for patch in parsed:
                patch["old"] = canonical_path(patch["old"])
                patch["new"] = canonical_path(patch["new"])
                path = patch["new"] if patch["new"] != "/dev/null" else patch["old"]
                # Resolve relative paths only against a proven session cwd.
                # Suffix/basename similarity is not file identity.
                canonical = path
                covered.update((patch["old"], patch["new"]))
                if patch["old"] not in {"/dev/null", path} and patch["new"] != "/dev/null":
                    old = patch["old"]
                    if old in models:
                        models[canonical] = models.pop(old)
                    if old in files:
                        files.pop(old)
                    if old in invalid:
                        invalid.add(canonical)
                if canonical not in files and len(files) >= MAX_FILES:
                    overflow = True
                    continue
                row = files.setdefault(canonical, {"path": canonical, "state": "unavailable"})
                if canonical in invalid:
                    continue
                try:
                    model = models.setdefault(canonical, _File(patch["old"], patch["old"]))
                    model.apply(patch)
                    diff, added, removed = model.render()
                    used += len(diff) - len(row.get("diff", ""))
                    if len(diff) > MAX_DIFF or used > MAX_TURN_DIFF:
                        raise ValueError("turn diff too large")
                    row.update(state="available", diff=diff, additions=added, deletions=removed)
                except ValueError:
                    row.update(state="unavailable", reason="改动记录不完整，无法还原本轮累计差异")
                    row.pop("diff", None)
                    models.pop(canonical, None)
                    invalid.add(canonical)
            for path in expected_paths:
                if path in covered or path not in files:
                    continue
                if native_complete:
                    # A complete cumulative snapshot omits reverted net-zero
                    # files; that is not a missing per-tool patch.
                    files[path].update(state="available", diff="", additions=0, deletions=0)
                else:
                    invalidate(path)
        except ValueError:
            for path in expected_paths:
                invalidate(path)
    for row in files.values():
        if row["state"] == "unavailable":
            row.setdefault("reason", "此历史记录未保存可验证的原生差异")
    return _payload(list(files.values()), truncated=overflow, tool_ids=list(tools)[:MAX_EVENTS])


def _payload(files: list[dict], *, truncated: bool = False, tool_ids: list[str] | None = None) -> dict:
    payload = {"version": _PAYLOAD_VERSION, "files": files[:MAX_FILES]}
    if tool_ids:
        payload["tool_ids"] = tool_ids
    if truncated:
        payload["truncated"] = True
    payload["revision"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    return payload


class TurnChangeTracker:
    """Small per-session live projection. Large process output is never retained."""

    def __init__(self, cwd: str | None = None):
        self.cwd = cwd
        self.turns: OrderedDict[str, OrderedDict[str, dict]] = OrderedDict()
        self.tool_owners: OrderedDict[str, str] = OrderedDict()
        self.last_revisions: dict[str, str] = {}
        self.incomplete: dict[str, dict] = {}

    def observe(self, event: dict, owner: str | None) -> tuple[str, dict] | None:
        kind = event.get("type")
        if kind not in {"tool_use", "tool_result", "turn_diff", "turn_end"}:
            return None
        tool_id = event.get("tool_use_id")
        turn_id = event.get("turn_id") or self.tool_owners.get(tool_id) or owner
        if not turn_id:
            return None
        if kind == "tool_use":
            if str(event.get("tool", "")).lower() not in MUTATORS:
                return None
            data = {"type": kind, "tool_use_id": tool_id, "tool": event["tool"],
                    "input": {"file_paths": paths_from_input(event.get("input"))}}
            self.tool_owners[tool_id] = turn_id
            while len(self.tool_owners) > 4096:
                self.tool_owners.popitem(last=False)
        elif kind == "tool_result":
            if tool_id not in self.tool_owners:
                return None
            data = {key: event[key] for key in (
                "type", "tool_use_id", "is_error", "diff", "diff_source", "diff_truncated", "truncated",
                "_file_diffs", "_files_truncated",
            ) if key in event}
            if isinstance(data.get("_file_diffs"), list):
                data.pop("diff", None)  # do not retain the duplicate tool-card patch
        elif kind == "turn_diff":
            data = {key: event[key] for key in ("type", "diff", "truncated") if key in event}
        else:
            if turn_id not in self.turns:
                return None
            data = {"type": kind}
        entries = self.turns.setdefault(turn_id, OrderedDict())
        self.turns.move_to_end(turn_id)
        while len(self.turns) > 8:
            old, _ = self.turns.popitem(last=False)
            self.last_revisions.pop(old, None)
            self.incomplete.pop(old, None)
            self.tool_owners = OrderedDict((key, value) for key, value in self.tool_owners.items() if value != old)
        if turn_id not in self.incomplete:
            entry_key = f"{kind}:{tool_id or ''}"
            entries[entry_key] = data
            # A replaced native cumulative snapshot belongs at its latest
            # source position, not where its first revision was inserted.
            entries.move_to_end(entry_key)
            source_size = sum(
                sum(len(entry.get("diff") or "") for entry in row["_file_diffs"])
                if isinstance(row.get("_file_diffs"), list) else len(row.get("diff") or "")
                for row in entries.values())
            if len(entries) > MAX_EVENTS or source_size > MAX_TURN_DIFF:
                # Once a required base is lost, later patches cannot resurrect
                # a misleading complete tail. Keep a bounded path tombstone.
                paths = list(dict.fromkeys(path for row in entries.values()
                                          for path in paths_from_input(row.get("input"))))
                self.incomplete[turn_id] = _payload([
                    {"path": path, "state": "unavailable", "reason": "本轮记录超过保存上限，未提供不完整差异"}
                    for path in paths[:MAX_FILES]
                ], truncated=True)
                entries.clear()
        payload = self.incomplete.get(turn_id)
        if payload is not None:
            paths = {row["path"] for row in payload["files"]}
            for path in paths_from_input(data.get("input")):
                if path not in paths and len(paths) < MAX_FILES:
                    payload["files"].append({"path": path, "state": "unavailable", "reason": "本轮记录超过保存上限"})
                    paths.add(path)
            payload = self.incomplete[turn_id] = _payload(payload["files"], truncated=True)
        else:
            payload = project_turn_changes(list(entries.values()), self.cwd)
        if not payload["files"] and turn_id not in self.last_revisions:
            return None
        if kind != "turn_end" and self.last_revisions.get(turn_id) == payload["revision"]:
            return None
        self.last_revisions[turn_id] = payload["revision"]
        return turn_id, payload


def change_summary(payload: dict) -> dict:
    files = payload["files"]
    available = [row for row in files if row["state"] == "available"]
    return {"revision": payload["revision"],
            "total_files": len(files),
            "total_additions": sum(row.get("additions", 0) for row in available),
            "total_deletions": sum(row.get("deletions", 0) for row in available),
            "next_offset": FILE_PAGE_SIZE if len(files) > FILE_PAGE_SIZE else None,
            **({"truncated": True} if payload.get("truncated") else {}), "files": [
        {key: value for key, value in row.items() if key != "diff"}
        for row in files[:FILE_PAGE_SIZE]
    ]}


class TurnChangeArchive:
    """Private immutable revisions. Later turns cannot mutate earlier payloads."""

    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir) / "turn-changes.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS turn_changes (sid TEXT, turn_id TEXT, revision TEXT, payload TEXT NOT NULL, final INTEGER NOT NULL, PRIMARY KEY(sid,turn_id,revision))")
            db.execute("CREATE TABLE IF NOT EXISTS turn_change_files (sid TEXT, turn_id TEXT, revision TEXT, position INTEGER, path TEXT, summary TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(sid,turn_id,revision,position), UNIQUE(sid,turn_id,revision,path))")
        self.path.chmod(0o600)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def put(self, sid: str, turn_id: str, payload: dict, *, final: bool = False) -> None:
        encoded = json.dumps(payload, ensure_ascii=True)
        if len(encoded) > MAX_DIFF * 8:
            raise ValueError("archive payload too large")
        with self._lock, self._connect() as db:
            if self.path.stat().st_size > MAX_ARCHIVE:
                raise ValueError("turn diff archive full")
            files = payload["files"]
            manifest = {key: value for key, value in payload.items() if key != "files"}
            summary = change_summary(payload)
            manifest["_file_index"] = {key: summary[key] for key in (
                "total_files", "total_additions", "total_deletions")}
            existed = db.execute("SELECT 1 FROM turn_changes WHERE sid=? AND turn_id=? AND revision=?",
                                 (sid, turn_id, payload["revision"])).fetchone()
            db.execute("INSERT INTO turn_changes VALUES (?,?,?,?,?) ON CONFLICT(sid,turn_id,revision) DO UPDATE SET final=max(final,excluded.final)",
                       (sid, turn_id, payload["revision"], json.dumps(manifest), int(final)))
            if not existed:
                db.executemany("INSERT INTO turn_change_files VALUES (?,?,?,?,?,?,?)", [
                    (sid, turn_id, payload["revision"], index, row["path"],
                     json.dumps({key: value for key, value in row.items() if key != "diff"}),
                     json.dumps(row)) for index, row in enumerate(files)
                ])
            # Running revisions are short-lived previews; completed revisions
            # are never evicted by later turns or the history projection LRU.
            db.execute("DELETE FROM turn_changes WHERE sid=? AND turn_id=? AND final=0 AND rowid NOT IN (SELECT rowid FROM turn_changes WHERE sid=? AND turn_id=? ORDER BY rowid DESC LIMIT 4)",
                       (sid, turn_id, sid, turn_id))
            db.execute("DELETE FROM turn_change_files WHERE sid=? AND turn_id=? AND revision NOT IN (SELECT revision FROM turn_changes WHERE sid=? AND turn_id=?)",
                       (sid, turn_id, sid, turn_id))

    def get(self, sid: str, turn_id: str, revision: str) -> dict | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT payload FROM turn_changes WHERE sid=? AND turn_id=? AND revision=?",
                             (sid, turn_id, revision)).fetchone()
            return self._hydrate(db, sid, turn_id, self._validated_payload(sid, row))

    def latest_final(self, sid: str, turn_id: str) -> dict | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT payload FROM turn_changes WHERE sid=? AND turn_id=? AND final=1 ORDER BY rowid DESC LIMIT 1",
                             (sid, turn_id)).fetchone()
            return self._hydrate(db, sid, turn_id, self._validated_payload(sid, row))

    @staticmethod
    def _hydrate(db, sid: str, turn_id: str, payload: dict | None) -> dict | None:
        if payload is None or "_file_index" not in payload:
            return payload
        index = payload.pop("_file_index")
        rows = db.execute("SELECT payload FROM turn_change_files WHERE sid=? AND turn_id=? AND revision=? ORDER BY position",
                          (sid, turn_id, payload["revision"])).fetchall()
        if len(rows) != index["total_files"]:
            raise ValueError("incomplete file archive")
        return {**payload, "files": [json.loads(row[0]) for row in rows]}

    def page(self, sid: str, turn_id: str, revision: str, offset: int = 0, limit: int = FILE_PAGE_SIZE) -> dict | None:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= FILE_PAGE_SIZE:
            raise ValueError("invalid file page")
        with self._lock, self._connect() as db:
            payload = self._validated_payload(sid, db.execute(
                "SELECT payload FROM turn_changes WHERE sid=? AND turn_id=? AND revision=?",
                (sid, turn_id, revision)).fetchone())
            if payload is None:
                return None
            index = payload.get("_file_index")
            if index is None:
                files = payload["files"]
                total = len(files)
                rows = [{key: value for key, value in row.items() if key != "diff"}
                        for row in files[offset:offset+limit]]
            else:
                total = index["total_files"]
                rows = [json.loads(row[0]) for row in db.execute(
                    "SELECT summary FROM turn_change_files WHERE sid=? AND turn_id=? AND revision=? AND position>=? ORDER BY position LIMIT ?",
                    (sid, turn_id, revision, offset, limit)).fetchall()]
            if offset > total or len(rows) != min(limit, total-offset):
                raise ValueError("incomplete file page")
            next_offset = offset + len(rows)
            return {"revision": revision, "offset": offset, "files": rows,
                    "total_files": total, "next_offset": next_offset if next_offset < total else None}

    def file(self, sid: str, turn_id: str, revision: str, path: str) -> dict | None:
        with self._lock, self._connect() as db:
            manifest = self._validated_payload(sid, db.execute(
                "SELECT payload FROM turn_changes WHERE sid=? AND turn_id=? AND revision=?",
                (sid, turn_id, revision)).fetchone())
            if manifest is None:
                return None
            if "_file_index" not in manifest:
                return next((row for row in manifest["files"] if row["path"] == path), None)
            row = db.execute("SELECT payload FROM turn_change_files WHERE sid=? AND turn_id=? AND revision=? AND path=?",
                             (sid, turn_id, revision, path)).fetchone()
            return json.loads(row[0]) if row else None

    @staticmethod
    def _validated_payload(sid: str, row: tuple | None) -> dict | None:
        if row is None:
            return None
        payload = json.loads(row[0])
        # Pre-v2 per-tool Codex archives lost truncation evidence. Keep their
        # immutable bytes, but rebuild from native history instead of allowing
        # an unverified capture to override a newly detected incomplete diff.
        # Native turn-only snapshots and Claude captures were not on this path.
        if (sid.startswith("codex:") and payload.get("tool_ids")
                and payload.get("version") not in (2, _PAYLOAD_VERSION)):
            return None
        return payload

    def history_summary(
        self, sid: str, turn: dict, events: list[dict], cwd: str | None,
        *, incomplete: bool = False,
    ) -> dict:
        """Attach source-only changes to every history projection family.

        Client-message aliases are exact identities, unlike native task ids
        which can own several steer segments. Copy a captured live revision
        under the historical visible id so GetDiff needs no identity guessing.
        """
        payload = project_turn_changes(events, cwd)
        if incomplete:
            payload = _payload([
                {"path": row["path"], "state": "unavailable",
                 "reason": "历史改动记录不完整，未提供部分累计差异"}
                for row in payload["files"]
            ], truncated=True)
        for identity in dict.fromkeys((turn["id"], turn.get("clientMsgId"))):
            if not identity:
                continue
            existing = self.latest_final(sid, identity)
            if existing is None:
                continue
            proven = {row["path"] for row in existing["files"]
                      if row["state"] == "available"}
            if (not payload["files"] or (
                any(row["state"] != "available" for row in payload["files"])
                and set(payload.get("tool_ids", ())) <= set(existing.get("tool_ids", ()))
                and all(row["path"] in proven for row in payload["files"])
            )):
                payload = existing
                if not payload["files"]:
                    break
        if payload["files"] or payload.get("tool_ids"):
            self.put(sid, turn["id"], payload, final=bool(turn.get("done")))
        return change_summary(payload)

    def rekey(self, old: str, new: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE OR IGNORE turn_change_files SET sid=? WHERE sid=?", (new, old))
            db.execute("DELETE FROM turn_change_files WHERE sid=?", (old,))
            db.execute("UPDATE OR IGNORE turn_changes SET sid=? WHERE sid=?", (new, old))
            db.execute("DELETE FROM turn_changes WHERE sid=?", (old,))

    def drop(self, sid: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM turn_change_files WHERE sid=?", (sid,))
            db.execute("DELETE FROM turn_changes WHERE sid=?", (sid,))
