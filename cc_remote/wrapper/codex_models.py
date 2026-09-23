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

from cc_remote import __version__
from cc_remote.log import logger
from cc_remote.wrapper.codex_runtime import (
    codex_env as _codex_env,
    resolve_codex_bin as _resolve_codex_bin,
)

log = logger("cc_remote.wrapper.codex_models")

# Internal effort/default lookups may reuse a catalog. Explicit picker requests
# refresh it: native availability can change independently of the CLI version.
_TTL = 600.0
_RPC_TIMEOUT = 30.0
_MAX_MODELS = 256
_MAX_EFFORTS = 16
_MAX_CATALOG_TEXT = 4096

_profile_cache: dict[str, tuple[list[dict], float]] = {}
_inflight: dict[str, asyncio.Task[list[dict]]] = {}

# Cost/latency order, low -> high. Used only to clamp an unsupported request DOWN
# to something the model accepts; unknown levels sort last so they never win.
EFFORT_ORDER = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
# Wire/display sentinel used only when app-server explicitly selected the
# model's default but model/list is temporarily unavailable. It is never sent
# back to turn/start as a reasoning effort.
MODEL_DEFAULT_EFFORT = "model-default"


def _rank(effort: str) -> int:
    return EFFORT_ORDER.index(effort) if effort in EFFORT_ORDER else len(EFFORT_ORDER)


def _profile_cache_key(codex_home: str | None) -> str:
    return (os.path.realpath(os.path.expanduser(codex_home))
            if codex_home else "")


async def _rpc_model_list(codex_home: str | None = None) -> list[dict]:
    """One-shot `codex app-server --stdio` -> initialize -> model/list -> die.
    No thread, no turn, no tokens."""
    binp = await asyncio.to_thread(_resolve_codex_bin)
    proc = await asyncio.create_subprocess_exec(
        binp, "app-server", "--stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=os.path.expanduser("~"),
        env=(_codex_env(binp) if codex_home is None
             else _codex_env(binp, codex_home)),
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
                    "params": {"clientInfo": {
                        "name": "cc-remote", "version": __version__,
                    }}})
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


async def codex_catalog(
    force: bool = False,
    *,
    codex_home: str | None = None,
) -> list[dict]:
    """Refresh on explicit reads; cache internal lookups by account home.

    Concurrent readers share one native query per account. A disconnected
    reader cannot cancel another reader's refresh, and a slow account cannot
    block other accounts. Discovery failures retain only this account's last
    good catalog (or []), without extending its cache lifetime.
    """
    cache_key = _profile_cache_key(codex_home)
    pending = _inflight.get(cache_key)
    if pending is None:
        cached, cached_ts = _profile_cache.get(cache_key, (None, 0.0))
        if not force and cached is not None and time.monotonic() - cached_ts < _TTL:
            return cached
        pending = asyncio.create_task(_refresh_catalog(cache_key, codex_home))
        _inflight[cache_key] = pending
    return await asyncio.shield(pending)


async def _refresh_catalog(cache_key: str, codex_home: str | None) -> list[dict]:
    cached, _ = _profile_cache.get(cache_key, (None, 0.0))
    try:
        data = _normalize(await _rpc_model_list(codex_home))
        if not data:
            log.warning("codex model/list returned nothing")
            return cached or []
        _profile_cache[cache_key] = (data, time.monotonic())
        log.info("codex catalog", count=len(data), ids=[m["id"] for m in data])
        return data
    except Exception as e:
        log.warning("codex model/list failed", error=str(e))
        return cached or []
    finally:
        _inflight.pop(cache_key, None)


async def efforts_for(
    model: Optional[str],
    *,
    codex_home: str | None = None,
) -> list[str]:
    if not model:
        return []
    for m in await codex_catalog(codex_home=codex_home):
        if m["id"] == model:
            return m["efforts"]
    return []


async def default_effort_for(
    model: Optional[str],
    *,
    codex_home: str | None = None,
) -> Optional[str]:
    if not model:
        return None
    for candidate in await codex_catalog(codex_home=codex_home):
        if candidate["id"] == model:
            value = candidate.get("default_effort")
            return value if isinstance(value, str) and value else None
    return None


async def clamp_effort(
    model: Optional[str], effort: Optional[str],
    *,
    codex_home: str | None = None,
) -> Optional[str]:
    """Coerce `effort` to something `model` actually supports.

    Clamps DOWN — the highest supported level at or below the request — so we never
    silently spend more than the user asked for. If the request is below everything
    the model offers, take its lowest. An unknown model (custom provider, or a
    catalog we couldn't read) passes through untouched: codex forwards it verbatim.
    """
    if not effort:
        return effort
    supported = await efforts_for(model, codex_home=codex_home)
    if not supported or effort in supported:
        return effort
    want = _rank(effort)
    below = [e for e in supported if _rank(e) <= want]
    picked = max(below, key=_rank) if below else min(supported, key=_rank)
    log.warning("codex effort not supported by model -> clamped",
                model=model, requested=effort, applied=picked, supported=supported)
    return picked
