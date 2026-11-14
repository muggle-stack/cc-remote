"""Persist explicit picker choices in Claude's own user settings.

The SDK's runtime setters do not persist picker defaults. Merge the native
model/modelSettings keys together from the latest disk contents. Mixing local
model writes with native update_settings can overwrite the model with the
CLI's cached settings. Never copy settings into Remote state or logs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import threading

from cc_remote.wrapper.claude_controls import valid_claude_model

_WRITE_LOCK = threading.Lock()
_MAX_BYTES = 1024 * 1024


def _read(path: Path) -> tuple[bytes | None, os.stat_result | None]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_size > _MAX_BYTES):
            raise ValueError("Claude user settings are not safely writable")
        raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError("Claude user settings are too large")
        return raw, info


def save_user_defaults(
    config_root: str | Path, *, model: str | None = None,
    effort: str | None = None, effort_model: str | None = None,
    switch_models_on_flag: bool | None = None,
) -> None:
    """Merge only explicit selections, preserving unrelated native settings.

    effort_model is the resolved model reported by the CLI control plane, not
    a provider response or a Remote alias guess. max is session-only.
    """
    if model is not None and valid_claude_model(model) is None:
        raise ValueError("Invalid Claude model selection")
    if effort == "max":
        effort = None
    if effort is not None:
        if effort not in {"low", "medium", "high", "xhigh"}:
            raise ValueError("Invalid persistent Claude effort")
        if valid_claude_model(effort_model) is None:
            raise ValueError("Claude did not report its resolved model")
        if effort_model.lower().endswith("[1m]"):
            effort_model = effort_model[:-4]
    if switch_models_on_flag is not None and not isinstance(switch_models_on_flag, bool):
        raise ValueError("Invalid Claude model fallback preference")
    if model is None and effort is None and switch_models_on_flag is None:
        return
    path = Path(config_root) / "settings.json"
    with _WRITE_LOCK:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for _attempt in range(4):
            raw, info = _read(path)
            settings = json.loads(raw) if raw and raw.strip() else {}
            if not isinstance(settings, dict):
                raise ValueError("Claude user settings must be an object")
            if model is not None:
                settings["model"] = model
            if switch_models_on_flag is not None:
                settings["switchModelsOnFlag"] = switch_models_on_flag
            if effort is not None:
                table = settings.setdefault("modelSettings", {})
                if not isinstance(table, dict):
                    raise ValueError("Invalid native model settings")
                entry = table.setdefault(effort_model, {})
                if not isinstance(entry, dict):
                    raise ValueError("Invalid native model effort settings")
                entry["effortLevel"] = effort
            payload = (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode()
            if len(payload) > _MAX_BYTES:
                raise ValueError("Claude user settings are too large")
            fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode) if info else 0o600)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                current, current_info = _read(path)
                if (current != raw or (info is not None and current_info is not None
                        and (info.st_dev, info.st_ino) != (
                            current_info.st_dev, current_info.st_ino))):
                    continue
                os.replace(temporary, path)
                return
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        raise RuntimeError("Claude user settings changed while saving")


def save_model_default(config_root: str | Path, model: str) -> None:
    save_user_defaults(config_root, model=model)


def automatic_model_fallback(config_root: str | Path) -> bool:
    """Read only the native preference; an unset preference asks in Remote."""
    # Native user settings may be a dotfile-manager symlink. Reading that
    # preference does not require the stricter identity checks used for writes.
    try:
        fd = os.open(Path(config_root).expanduser() / "settings.json", os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
                return False
            raw = stream.read(_MAX_BYTES + 1)
        settings = json.loads(raw) if raw and raw.strip() else {}
    except (OSError, ValueError):
        return False
    return isinstance(settings, dict) and settings.get("switchModelsOnFlag") is True
