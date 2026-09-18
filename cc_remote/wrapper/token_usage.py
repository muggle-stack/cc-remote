"""Bounded, replace-only token accounting from native usage (never text estimates)."""
from __future__ import annotations

from collections import OrderedDict
import re

from cc_remote.protocol import MAX_SAFE_WIRE_INTEGER, TokenUsage, TurnUsage


def count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= MAX_SAFE_WIRE_INTEGER else None


def native_usage(raw: object, engine: str, *, output: bool = True) -> TokenUsage | None:
    if not isinstance(raw, dict):
        return None
    if engine == "claude":
        keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    else:
        keys = ("inputTokens", "outputTokens", "cachedInputTokens" if engine == "codex" else "cacheReadTokens", "cacheWriteTokens")
    values = [count(raw.get(key)) for key in keys]
    if not output:
        values[1] = None
    # Claude and DSH report disjoint cache/input categories; Codex includes
    # cached input in inputTokens already. Missing optional cache fields are 0.
    if values[0] is not None and engine != "codex":
        values[0] = count(values[0] + (values[2] or 0) + (values[3] or 0))
    if all(value is None for value in values):
        return None
    return TokenUsage(input_tokens=values[0], output_tokens=values[1],
                      cache_read_tokens=values[2], cache_write_tokens=values[3])


class UsageLedger:
    """Each native response owns one cumulative sample, even after replay.

    Never evict individual samples within a turn: their tombstones prevent a
    replayed response from being counted twice. Excess samples are ignored.
    """

    def __init__(self):
        self.turns: OrderedDict[str, dict[str, TokenUsage]] = OrderedDict()
        self.totals: dict[str, TokenUsage] = {}

    def update(self, owner: str | None, key: str, usage: TokenUsage | None) -> list[TurnUsage]:
        if not owner or usage is None:
            return []
        if owner not in self.turns:
            self.turns[owner] = {}
            if len(self.turns) > 32:
                old, _ = self.turns.popitem(last=False)
                self.totals.pop(old, None)
        samples = self.turns[owner]
        if key not in samples and len(samples) >= 4096:
            return []
        previous = samples.get(key, TokenUsage())
        merged = previous.model_copy(update={
            name: max(value, getattr(previous, name) or 0)
            for name, value in usage.model_dump().items() if value is not None
        })
        if merged == previous:
            return []
        samples[key] = merged
        fields = {}
        for name in TokenUsage.model_fields:
            values = [getattr(sample, name) for sample in samples.values()
                      if getattr(sample, name) is not None]
            fields[name] = count(sum(values)) if values else None
        total = TokenUsage(**fields)
        return self.replace(owner, total)

    def replace(self, owner: str | None, total: TokenUsage | None) -> list[TurnUsage]:
        if not owner or total is None or self.totals.get(owner) == total:
            return []
        self.totals[owner] = total
        return [TurnUsage(turn_id=owner, usage=total)]

    def settle(self, owner: str, provisional: str, durable: str) -> None:
        samples = self.turns.get(owner, {})
        sample = samples.pop(provisional, None)
        if sample is not None:
            samples.setdefault(durable, sample)


class CodexUsageTracker:
    """Difference the native thread totals at exact turn boundaries.

    A cold attachment without a baseline starts with the latest reported
    request. Later updates use cumulative totals, so duplicate notifications
    and identical consecutive requests remain distinguishable.
    """

    def __init__(self):
        self.previous: TokenUsage | None = None
        self.owner: str | None = None
        self.total = TokenUsage()

    def feed(self, message: dict) -> TurnUsage | None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            return None
        turn = params.get("turn")
        owner = params.get("turnId") or (turn.get("id") if isinstance(turn, dict) else None)
        if owner is not None and (not isinstance(owner, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}", owner)):
            return None
        if method == "thread/compacted":
            self.previous = None
            return None
        if method == "turn/started" and isinstance(owner, str) and owner != self.owner:
            self.owner, self.total = owner, TokenUsage()
        if method != "thread/tokenUsage/updated" or not isinstance(owner, str):
            return None
        if self.owner is not None and owner != self.owner:
            return None  # a delayed update must not reset the active baseline
        raw = params.get("tokenUsage")
        if not isinstance(raw, dict):
            return None
        current = native_usage(raw.get("total"), "codex")
        last = native_usage(raw.get("last"), "codex")
        if owner != self.owner:
            self.owner, self.total = owner, TokenUsage()
            self.previous = None  # joining an already-active native turn
        if current is None:
            return None  # without cumulative totals repeated requests are ambiguous
        values = {}
        for name in TokenUsage.model_fields:
            new = getattr(current, name)
            old = getattr(self.previous, name) if self.previous else None
            delta = new - old if new is not None and old is not None and new >= old else (
                getattr(last, name) if last else None)
            values[name] = count((getattr(self.total, name) or 0) + delta) if delta is not None else getattr(self.total, name)
        self.previous = current
        total = TokenUsage(**values)
        if total == self.total:
            return None
        self.total = total
        return TurnUsage(turn_id=owner, usage=total)
