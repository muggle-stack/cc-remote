"""Scoped session selection, matching web/src/session-order.ts."""

from datetime import datetime, timezone
import math

SCOPES = {
    (engine, space) for engine in ("claude", "codex") for space in ("code", "work")
}


def activity(row: dict) -> float:
    value = row.get("last_modified")
    if value is None or value == "":
        return -math.inf
    try:
        number = float(value)
        if math.isfinite(number):
            return number if number > 10_000_000_000 else number * 1000
    except (ValueError, TypeError):
        pass
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp() * 1000
    except (ValueError, OverflowError, OSError):
        return -math.inf


def scoped_catalog(catalog: dict, engine: str, space: str) -> dict:
    return dict(
        sorted(
            (
                (sid, row)
                for sid, row in catalog.items()
                if (row.get("engine") or engine, row.get("space") or "code")
                == (engine, space)
            ),
            key=lambda item: (-activity(item[1]), item[0]),
        )
    )


def select_session(catalog: dict, remembered: str | None) -> str | None:
    if remembered in catalog:
        return remembered
    return next(
        (sid for sid, row in catalog.items() if row.get("tag") != "archived"),
        next(iter(catalog), None),
    )
