"""Private, endpoint-scoped tab identities; no transcript or credentials."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile


def clean(value):
    if not isinstance(value, dict):
        return {"tabs": [], "active": None, "scope": ["codex", "code"]}
    tabs = value.get("tabs", [])
    tabs = list(dict.fromkeys(
        sid for sid in tabs[:1024]
        if isinstance(sid, str) and 0 < len(sid) <= 256
        and not any(ord(char) < 32 for char in sid)
        and not sid.startswith(("tmp-", "btw-"))
    )) if isinstance(tabs, list) else []
    scope = value.get("scope")
    if (not isinstance(scope, list) or len(scope) != 2
            or scope[0] not in ("codex", "claude")
            or scope[1] not in ("code", "work")):
        scope = ["codex", "code"]
    return {"tabs": tabs, "active": value.get("active")
            if value.get("active") in tabs else None, "scope": scope}


class TabStore:
    def __init__(self, url, machine, username, *, directory=None):
        base = Path(directory) if directory else Path(
            os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state"
        ) / "cc-remote" / "tui-tabs"
        identity = json.dumps([url, machine, username]).encode()
        self.path = base / (hashlib.sha256(identity).hexdigest() + ".json")
        self.previous = clean(None)

    def _read(self):
        try:
            # Bound corrupted/untrusted local state before decoding it.
            with self.path.open("rb") as stream:
                raw = stream.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                raise ValueError("TUI tab state exceeds size limit")
            return clean(json.loads(raw))
        except FileNotFoundError:
            return None

    def load(self):
        value = self._read()
        self.previous = value or clean(None)
        return value

    def save(self, tabs, active, scope):
        current = clean({"tabs": tabs, "active": active, "scope": list(scope)})
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_suffix(".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            latest = self._read() or clean(None)
            removed = set(self.previous["tabs"]) - set(current["tabs"])
            added = [sid for sid in current["tabs"]
                     if sid not in self.previous["tabs"]]
            # Merge local edits, not a stale whole-window snapshot. A second
            # terminal must not resurrect tabs another terminal just closed.
            merged = list(dict.fromkeys(
                [sid for sid in latest["tabs"] if sid not in removed] + added
            ))
            result = clean({**current, "tabs": merged})
            if result["active"] is None and latest["active"] in merged:
                result["active"] = latest["active"]
                result["scope"] = latest["scope"]
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", dir=self.path.parent, delete=False,
                    prefix=".tabs-", encoding="utf-8",
                ) as stream:
                    temporary = stream.name
                    json.dump(result, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
        self.previous = current
