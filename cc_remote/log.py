"""Structured JSON logging with token redaction.

Tokens are never logged: the Authorization header and any field whose name
matches a redact key is replaced with "***" before serialization. Use
`logger("cc_remote.wrapper")` and pass structured fields as kwargs:
    log.info("connected", url=relay_url, role="wrapper")
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from itertools import islice
from typing import Any

_REDACT_KEYS = {
    "token", "authorization", "auth_token", "client_token", "wrapper_token",
    "api_key", "anthropic_auth_token", "anthropic_api_key", "password",
    "secret", "bearer", "prompt", "line",
}

_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
_SECRET_NAME = (
    r"(?:authorization|[a-z0-9_-]*(?:token|password|secret|api[_-]?key))"
)
_QUOTED_SECRET_RE = re.compile(
    rf"(?i)(?P<prefix>[\"']?\b{_SECRET_NAME}\b[\"']?\s*[:=]\s*)"
    rf"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)"
)
_UNQUOTED_SECRET_RE = re.compile(
    rf"(?i)(?P<prefix>\b{_SECRET_NAME}\b\s*[:=]\s*)"
    r"(?P<value>[^\s,;&}\]\"']+)"
)
_CC_STDERR_RE = re.compile(r"(?is)^\s*cc\s+stderr\s*:.*$")
_MAX_LOG_STRING = 8192
_MAX_LOG_ITEMS = 64
_MAX_LOG_DEPTH = 6


def _is_redact_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.lower().replace("-", "_")
    return (
        normalized in _REDACT_KEYS
        or normalized.endswith(("_token", "_password", "_secret", "_api_key"))
    )


def redact_text(value: str) -> str:
    """Redact common credential forms from log messages and scalar fields."""
    truncated = len(value) > _MAX_LOG_STRING
    value = value[:_MAX_LOG_STRING]
    if _CC_STDERR_RE.match(value):
        return "cc stderr: ***"
    value = _BEARER_RE.sub(r"\1***", value)
    value = _QUOTED_SECRET_RE.sub(
        lambda match: (
            match.group("prefix")
            + match.group("quote")
            + "***"
            + match.group("quote")
        ),
        value,
    )
    value = _UNQUOTED_SECRET_RE.sub(r"\g<prefix>***", value)
    return value + ("…[truncated]" if truncated else "")


def redact(obj: Any, _depth: int = 0) -> Any:
    if _depth >= _MAX_LOG_DEPTH:
        return f"<{type(obj).__name__}>"
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for key, value in islice(obj.items(), _MAX_LOG_ITEMS):
            safe_key = (redact_text(key) if isinstance(key, str)
                        else f"<{type(key).__name__}>")
            result[safe_key[:256]] = (
                "***" if _is_redact_key(key) else redact(value, _depth + 1)
            )
        if len(obj) > _MAX_LOG_ITEMS:
            result["_truncated"] = True
        return result
    if isinstance(obj, list):
        result = [redact(x, _depth + 1) for x in islice(obj, _MAX_LOG_ITEMS)]
        if len(obj) > _MAX_LOG_ITEMS:
            result.append("…[truncated]")
        return result
    if isinstance(obj, tuple):
        result = tuple(redact(x, _depth + 1) for x in islice(obj, _MAX_LOG_ITEMS))
        return result + (("…[truncated]",) if len(obj) > _MAX_LOG_ITEMS else ())
    if isinstance(obj, str):
        return redact_text(obj)
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    # Structured fields should contain JSON-like values. Never invoke a custom
    # object's potentially expensive or secret-bearing __str__ while logging.
    return f"<{type(obj).__name__}>"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_text(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        extra = getattr(record, "extra_data", None)
        if extra:
            for k, v in extra.items():
                payload[k] = "***" if _is_redact_key(k) else redact(v)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup(name: str = "cc_remote", level: str | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(JsonFormatter())
    logger.addHandler(h)
    logger.setLevel(level or os.environ.get("LOG_LEVEL", "INFO"))
    logger.propagate = False
    return logger


class _Logger:
    """Thin adapter: kwargs become structured fields on the JSON log line."""

    def __init__(self, raw: logging.Logger):
        self._raw = raw

    def _emit(self, level: int, msg: str, *, exc_info: bool = False, **kw: Any) -> None:
        self._raw.log(level, msg, exc_info=exc_info, extra={"extra_data": kw})

    def debug(self, msg: str, **kw: Any) -> None:
        self._emit(logging.DEBUG, msg, **kw)

    def info(self, msg: str, **kw: Any) -> None:
        self._emit(logging.INFO, msg, **kw)

    def warning(self, msg: str, **kw: Any) -> None:
        self._emit(logging.WARNING, msg, **kw)

    def error(self, msg: str, **kw: Any) -> None:
        self._emit(logging.ERROR, msg, **kw)

    def exception(self, msg: str, **kw: Any) -> None:
        self._emit(logging.ERROR, msg, exc_info=True, **kw)


def logger(name: str = "cc_remote") -> _Logger:
    return _Logger(setup(name))
