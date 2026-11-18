"""Discover models and reasoning capabilities through Codex's app-server.

`model/list` supplies model identifiers, descriptions, supported reasoning
efforts and their defaults. Preserve these native values for validation and
presentation, with account/provider-scoped caching when discovery is unavailable.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Optional

from cc_remote import __version__
from cc_remote.log import logger
from cc_remote.wrapper.codex_runtime import (
    codex_env as _codex_env,
    resolve_codex_bin as _resolve_codex_bin,
)
from cc_remote.wrapper.codex_sessions import codex_current_provider

log = logger("cc_remote.wrapper.codex_models")

# Internal effort/default lookups may reuse a catalog. Explicit picker requests
# refresh it: native availability can change independently of the CLI version.
_TTL = 600.0
_RPC_TIMEOUT = 30.0
_MAX_MODELS = 256
_MAX_EFFORTS = 16
_MAX_CATALOG_TEXT = 4096
_MAX_SERVICE_TIERS = 16
_SERVICE_TIER_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def normalize_service_tier(value: str | None) -> str:
    """Keep native tier ids; ``fast`` is the CLI alias for ``priority``."""
    if not value or value == "default":
        return "default"
    return "priority" if value == "fast" else value


def _service_tiers(model: dict) -> list[dict[str, str]] | None:
    # Missing metadata is unknown, not an authoritative empty entitlement list.
    raw = model.get("serviceTiers")
    if not isinstance(raw, list):
        return None
    result: list[dict[str, str]] = []
    seen = {"default", "toggle"}
    for item in raw:
        if not isinstance(item, dict):
            continue
        tier = item.get("id")
        if not isinstance(tier, str) or not _SERVICE_TIER_ID.fullmatch(tier):
            continue
        tier = normalize_service_tier(tier)
        if tier in seen:
            continue
        seen.add(tier)
        result.append({
            "id": tier,
            "name": str(item.get("name") or tier)[:128],
            "description": str(item.get("description") or "")[:_MAX_CATALOG_TEXT],
        })
        if len(result) >= _MAX_SERVICE_TIERS:
            break
    return result


def resolve_service_tier(
    model: str | None, tier: str | None, catalog: list[dict],
) -> str | None:
    """Validate a paid tier against this account AND model before changing it."""
    normalized = normalize_service_tier(tier)
    if normalized == "default":
        return None
    entry = next((m for m in catalog if m.get("id") == model), None)
    if entry is None or entry.get("service_tiers") is None:
        raise ValueError("未读取到当前模型的速度档位，请刷新模型列表后重试。")
    if not any(t.get("id") == normalized for t in entry["service_tiers"]):
        raise ValueError("当前账号的所选模型不支持此速度档位，请重新选择。")
    return normalized

_profile_cache: dict[tuple[str, str], tuple[list[dict], float]] = {}
_inflight: dict[tuple[str, str], asyncio.Task[list[dict]]] = {}

# Wire/display sentinel used only when app-server explicitly selected the
# model's default but model/list is temporarily unavailable. It is never sent
# back to turn/start as a reasoning effort.
MODEL_DEFAULT_EFFORT = "model-default"


def _profile_cache_key(codex_home: str | None, model_provider: str) -> tuple[str, str]:
    home = os.path.realpath(os.path.expanduser(codex_home)) if codex_home else ""
    return home, model_provider


async def _rpc_model_list(
    codex_home: str | None = None, *, model_provider: str | None = None,
) -> list[dict]:
    """One-shot `codex app-server --stdio` -> initialize -> model/list -> die.
    No thread, no turn, no tokens."""
    binp = await asyncio.to_thread(_resolve_codex_bin)
    argv = [binp, "app-server", "--stdio"]
    if model_provider:
        # The process-only override also scopes discovery on older app-servers
        # which predate model/list.modelProvider. User configuration is untouched.
        argv.extend(["-c", "model_provider=" + json.dumps(model_provider)])
    proc = await asyncio.create_subprocess_exec(
        *argv,
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
        result: list[dict] = []
        cursor = None
        seen: set[str] = set()
        for rid in range(2, 18):
            params: dict = {"limit": _MAX_MODELS}
            if model_provider:
                params["modelProvider"] = model_provider
            if cursor:
                params["cursor"] = cursor
            await send({"jsonrpc": "2.0", "id": rid,
                        "method": "model/list", "params": params})
            res = await asyncio.wait_for(await_result(rid), _RPC_TIMEOUT)
            result.extend((res or {}).get("data") or [])
            cursor = (res or {}).get("nextCursor")
            if not cursor:
                return result
            if not isinstance(cursor, str) or cursor in seen or len(result) >= _MAX_MODELS:
                raise RuntimeError("Codex model catalog pagination exceeded bounds")
            seen.add(cursor)
        raise RuntimeError("Codex model catalog pagination exceeded bounds")
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
        supported = [e for e in (m.get("supportedReasoningEfforts") or [])
                     if isinstance(e, dict) and e.get("reasoningEffort")][:_MAX_EFFORTS]
        efforts = [str(e["reasoningEffort"])[:64] for e in supported]
        out.append({
            "id": mid,
            "display_name": str(m.get("displayName") or mid)[:_MAX_CATALOG_TEXT],
            "description": str(m.get("description") or "")[:_MAX_CATALOG_TEXT],
            "efforts": efforts,
            "effort_descriptions": {
                effort: str(entry.get("description") or "")[:_MAX_CATALOG_TEXT]
                for effort, entry in zip(efforts, supported)
            },
            "default_effort": (
                str(m.get("defaultReasoningEffort"))[:64]
                if m.get("defaultReasoningEffort") else None),
            "is_default": bool(m.get("isDefault")),
            "service_tiers": _service_tiers(m),
            "default_service_tier": normalize_service_tier(
                m.get("defaultServiceTier")
                if isinstance(m.get("defaultServiceTier"), str) else None)[:64],
        })
        if len(out) >= _MAX_MODELS:
            break
    return out


async def codex_catalog(
    force: bool = False,
    *,
    codex_home: str | None = None,
    model_provider: str | None = None,
) -> list[dict]:
    """Refresh on explicit reads; cache by account home and native provider.

    Concurrent readers share one native query per provider/account. A disconnected
    reader cannot cancel another reader's refresh, and a slow account cannot
    block other providers/accounts. Failures retain only this scope's last
    good catalog (or []), without extending its cache lifetime.
    """
    if model_provider is None:
        configured_provider = await asyncio.to_thread(
            codex_current_provider, **({"codex_home": codex_home} if codex_home else {}),
        ) or "openai"
        # Let app-server resolve native profiles and managed configuration for
        # new sessions. A default read must never force our inferred provider
        # onto the process or populate an explicit thread provider's cache.
        cache_provider = "\0native-default:" + configured_provider
    elif (not isinstance(model_provider, str) or not model_provider.strip()
            or len(model_provider) > 256
            or any(ord(char) < 32 for char in model_provider)):
        raise ValueError("invalid Codex model provider")
    else:
        model_provider = model_provider.strip()
        cache_provider = model_provider
    cache_key = _profile_cache_key(codex_home, cache_provider)
    pending = _inflight.get(cache_key)
    if pending is None:
        cached, cached_ts = _profile_cache.get(cache_key, (None, 0.0))
        if not force and cached is not None and time.monotonic() - cached_ts < _TTL:
            return cached
        pending = asyncio.create_task(_refresh_catalog(cache_key, codex_home, model_provider))
        _inflight[cache_key] = pending
    return await asyncio.shield(pending)


async def _refresh_catalog(
    cache_key: tuple[str, str], codex_home: str | None, model_provider: str | None,
) -> list[dict]:
    cached, _ = _profile_cache.get(cache_key, (None, 0.0))
    try:
        data = _normalize(await _rpc_model_list(codex_home, model_provider=model_provider))
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
    model_provider: str | None = None,
) -> list[str]:
    if not model:
        return []
    for m in await codex_catalog(codex_home=codex_home, model_provider=model_provider):
        if m["id"] == model:
            return m["efforts"]
    return []


async def default_effort_for(
    model: Optional[str],
    *,
    codex_home: str | None = None,
    model_provider: str | None = None,
) -> Optional[str]:
    if not model:
        return None
    for candidate in await codex_catalog(codex_home=codex_home, model_provider=model_provider):
        if candidate["id"] == model:
            value = candidate.get("default_effort")
            return value if isinstance(value, str) and value else None
    return None


async def clamp_effort(
    model: Optional[str], effort: Optional[str],
    *,
    codex_home: str | None = None,
    model_provider: str | None = None,
) -> Optional[str]:
    """Keep supported choices, using the native default for a stale override.

    Unknown models or unavailable catalogs retain explicit choices for native
    validation. If a known model has no advertised default, clear the unsupported
    override so app-server can select its default without a local guess.
    """
    if not effort or not model:
        return effort
    entry = next((m for m in await codex_catalog(codex_home=codex_home, model_provider=model_provider)
                  if m["id"] == model), None)
    if entry is None:
        return effort
    supported = entry["efforts"]
    if effort in supported:
        return effort
    picked = entry.get("default_effort")
    if not picked or picked not in supported:
        picked = None
    log.warning("codex unsupported effort replaced with native default",
                model=model, requested=effort, applied=picked, supported=supported)
    return picked
