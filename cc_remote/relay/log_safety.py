"""Uvicorn/WebSocket log redaction for authentication-bearing request data."""
from __future__ import annotations

import copy
import logging
import re
from typing import Any

from uvicorn.config import LOGGING_CONFIG

_QUERY_TARGET = re.compile(
    r"(?P<base>(?:[a-z][a-z0-9+.-]*://[^\s/?\"']+)?/[^\s?\"']*)\?[^\s\"']*",
    re.IGNORECASE,
)
_JSON_SECRET = re.compile(
    r'''(?i)(["'](?:token|password|secret|answer|authorization|cookie)["']\s*:\s*["'])[^"']*(["'])'''
)
_NAMED_SECRET = re.compile(
    r"(?i)(\b(?:token|password|secret|answer|authorization|cookie|set-cookie)\b\s*[:=]\s*)([^\s,;}]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;}]+")
_SENSITIVE_KEYS = frozenset({
    "token", "password", "secret", "answer", "authorization", "cookie",
    "set-cookie",
})
_FILTER_NAME = "cc_remote_sensitive_log_redaction"


def redact_log_text(value: str) -> str:
    """Remove request queries and common credential fields from one log value."""
    redacted = _QUERY_TARGET.sub(r"\g<base>?[redacted]", value)
    redacted = _JSON_SECRET.sub(r"\1[redacted]\2", redacted)
    redacted = _BEARER.sub("Bearer [redacted]", redacted)
    return _NAMED_SECRET.sub(r"\1[redacted]", redacted)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_log_text(value)
    if isinstance(value, bytes):
        return redact_log_text(value.decode("utf-8", "replace")).encode("utf-8")
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: ("[redacted]" if str(key).lower() in _SENSITIVE_KEYS
                  else _redact_value(item))
            for key, item in value.items()
        }
    return value


class SensitiveLogFilter(logging.Filter):
    """Fail-closed redaction that runs before Uvicorn formats a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = _redact_value(record.msg)
            record.args = _redact_value(record.args)
            if hasattr(record, "scope"):
                record.scope = _redact_value(record.scope)
        except Exception:
            # Logging must never crash the relay or fall back to the unsafe raw
            # record merely because a dependency attached an unusual argument.
            record.msg = "log record redacted"
            record.args = ()
        return True


def uvicorn_log_config() -> dict[str, Any]:
    """Return Uvicorn's normal config with redaction on every output handler."""
    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})[_FILTER_NAME] = {"()": SensitiveLogFilter}
    for handler in config.get("handlers", {}).values():
        filters = list(handler.get("filters") or [])
        if _FILTER_NAME not in filters:
            filters.append(_FILTER_NAME)
        handler["filters"] = filters
    return config
