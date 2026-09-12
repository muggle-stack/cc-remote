"""Bounded product copy for an explicitly reported Codex usage limit."""
from __future__ import annotations

import re
from datetime import datetime


USAGE_LIMIT_FAILURE = (
    "本轮使用的 Codex 账号额度已用完。"
    "可切换账号、补充额度，或等待恢复后重试。"
)
_RETRY_PREFIX = "官方提示可于 "
_RETRY_SUFFIX = "（设备当地时间）重试。"
_RETRY_TIME = re.compile(
    r"\btry again at (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"([0-9]{1,2})(?:st|nd|rd|th)?, ([0-9]{4}) "
    r"([0-9]{1,2}):([0-9]{2}) ([AP]M)(?:\.|$)", re.IGNORECASE,
)
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_SAFE_RETRY_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}")


def usage_limit_failure(error: dict) -> str | None:
    """Recognize live camelCase and durable snake_case native errors.

    The official diagnostic formats its retry date in the device's local time.
    Preserve that date without guessing a timezone or consulting a newer quota
    snapshot, which could already belong to a different account.
    """
    tags = {"usageLimitExceeded", "usage_limit_exceeded"}
    message = error.get("message")
    message = message[:8192] if isinstance(message, str) else ""
    explicit = any(
        (isinstance(info, str) and info in tags)
        or (isinstance(info, dict) and any(tag in info for tag in tags))
        for info in (error.get("codexErrorInfo"), error.get("codex_error_info"))
    )
    if not explicit and not message.lower().startswith("you've hit your usage limit."):
        return None
    match = _RETRY_TIME.search(message)
    if match is None:
        return USAGE_LIMIT_FAILURE
    month, day, year, hour, minute, period = match.groups()
    if not 1 <= int(hour) <= 12:
        return USAGE_LIMIT_FAILURE
    try:
        retry_at = datetime(
            int(year), _MONTHS.index(month.lower()) + 1, int(day),
            int(hour) % 12 + (12 if period.upper() == "PM" else 0), int(minute),
        )
    except ValueError:
        return USAGE_LIMIT_FAILURE
    return (USAGE_LIMIT_FAILURE + _RETRY_PREFIX
            + retry_at.isoformat(sep=" ", timespec="minutes") + _RETRY_SUFFIX)


def is_usage_limit_failure(message: str) -> bool:
    """Allow only our copy and a validated date through history sanitization."""
    if message == USAGE_LIMIT_FAILURE:
        return True
    prefix = USAGE_LIMIT_FAILURE + _RETRY_PREFIX
    if not message.startswith(prefix) or not message.endswith(_RETRY_SUFFIX):
        return False
    retry_at = message[len(prefix):-len(_RETRY_SUFFIX)]
    if _SAFE_RETRY_TIME.fullmatch(retry_at) is None:
        return False
    try:
        datetime.fromisoformat(retry_at)
    except ValueError:
        return False
    return True
