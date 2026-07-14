"""The codex app-server IS the model catalog — ask it, never hardcode it.

`model/list` reports, per model, `supportedReasoningEfforts` and
`defaultReasoningEffort`. That is the ONLY source of truth: `turn/start` does not
validate `effort` at the RPC layer (measured — it happily accepts `bogus-zzz` and
starts the turn), so an unsupported level only blows up later, inside the model
API, as a failed turn.

We learned this the hard way by hardcoding the GPT-5.6 effort table and getting it
wrong twice: `minimal` doesn't exist on any model, and `gpt-5.6-luna` tops out at
`max`, not `ultra`. The catalog also moves under us — 0.143.0 knew nothing of the
5.6 family, 0.144.1 dropped `gpt-5.3-codex`.

Presentation (Chinese blurbs, icons) stays in the web's data.ts, keyed by id; we
ship `display_name`/`description` from the server as the fallback for models the
web has never heard of. So a new codex model shows up in the UI on its own.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

from cc_remote.log import logger
from cc_remote.wrapper.codex_handle import _codex_env, _resolve_codex_bin

log = logger("cc_remote.wrapper.codex_models")

_TTL = 600.0          # catalog is baked into the binary; re-probe only if it's upgraded
_RPC_TIMEOUT = 30.0
_MAX_MODELS = 256
_MAX_EFFORTS = 16
_MAX_CATALOG_TEXT = 4096

_cache: Optional[list[dict]] = None
_cache_ts: float = 0.0
_lock = asyncio.Lock()

# Cost/latency order, low -> high. Used only to clamp an unsupported request DOWN
# to something the model accepts; unknown levels sort last so they never win.
EFFORT_ORDER = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


def _rank(effort: str) -> int:
    return EFFORT_ORDER.index(effort) if effort in EFFORT_ORDER else len(EFFORT_ORDER)


async def _rpc_model_list() -> list[dict]:
    """One-shot `codex app-server --stdio` -> initialize -> model/list -> die.
    No thread, no turn, no tokens."""
    binp = await asyncio.to_thread(_resolve_codex_bin)
    proc = await asyncio.create_subprocess_exec(
        binp, "app-server", "--stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=os.path.expanduser("~"),
        env=_codex_env(binp),
        limit=16 * 1024 * 1024,
    )

    async def send(obj: dict) -> None:
        proc.stdin.write((json.dumps(obj) + "\n").encode())
        await proc.stdin.drain()

    async def await_result(rid: int):
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError("codex app-server closed before responding")
            try:
                m = json.loads(line)
            except Exception:
                continue
            if m.get("id") == rid and "method" not in m:
                if "error" in m:
                    raise RuntimeError(str(m["error"]))
                return m.get("result")

    try:
        await send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"clientInfo": {"name": "cc-remote", "version": "0.1.0"}}})
        await asyncio.wait_for(await_result(1), _RPC_TIMEOUT)
        await send({"jsonrpc": "2.0", "method": "initialized"})
        await send({"jsonrpc": "2.0", "id": 2, "method": "model/list", "params": {}})
        res = await asyncio.wait_for(await_result(2), _RPC_TIMEOUT)
        return (res or {}).get("data") or []
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


def _normalize(raw: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        if m.get("hidden"):
            continue                       # codex's own TUI hides these too
        mid = m.get("id") or m.get("model")
        if not mid:
            continue
        mid = str(mid)[:256]
        efforts = [str(e.get("reasoningEffort"))[:64]
                   for e in (m.get("supportedReasoningEfforts") or [])
                   if isinstance(e, dict) and e.get("reasoningEffort")][:_MAX_EFFORTS]
        out.append({
            "id": mid,
            "display_name": str(m.get("displayName") or mid)[:_MAX_CATALOG_TEXT],
            "description": str(m.get("description") or "")[:_MAX_CATALOG_TEXT],
            "efforts": efforts,
            "default_effort": (
                str(m.get("defaultReasoningEffort"))[:64]
                if m.get("defaultReasoningEffort") else None),
            "is_default": bool(m.get("isDefault")),
        })
        if len(out) >= _MAX_MODELS:
            break
    return out


async def codex_catalog(force: bool = False) -> list[dict]:
    """Normalized model list, cached. Never raises: on failure we serve the last
    good catalog (or []), and the web falls back to its static table."""
    global _cache, _cache_ts
    async with _lock:
        if not force and _cache is not None and (time.time() - _cache_ts) < _TTL:
            return _cache
        try:
            data = _normalize(await _rpc_model_list())
        except Exception as e:
            log.warning("codex model/list failed", error=str(e))
            return _cache or []
        if not data:
            log.warning("codex model/list returned nothing")
            return _cache or []
        _cache, _cache_ts = data, time.time()
        log.info("codex catalog", count=len(data), ids=[m["id"] for m in data])
        return data


async def efforts_for(model: Optional[str]) -> list[str]:
    if not model:
        return []
    for m in await codex_catalog():
        if m["id"] == model:
            return m["efforts"]
    return []


async def clamp_effort(model: Optional[str], effort: Optional[str]) -> Optional[str]:
    """Coerce `effort` to something `model` actually supports.

    Clamps DOWN — the highest supported level at or below the request — so we never
    silently spend more than the user asked for. If the request is below everything
    the model offers, take its lowest. An unknown model (custom provider, or a
    catalog we couldn't read) passes through untouched: codex forwards it verbatim.
    """
    if not effort:
        return effort
    supported = await efforts_for(model)
    if not supported or effort in supported:
        return effort
    want = _rank(effort)
    below = [e for e in supported if _rank(e) <= want]
    picked = max(below, key=_rank) if below else min(supported, key=_rank)
    log.warning("codex effort not supported by model -> clamped",
                model=model, requested=effort, applied=picked, supported=supported)
    return picked
