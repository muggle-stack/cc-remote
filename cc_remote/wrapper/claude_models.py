"""Discover models from the daily Claude CLI's initialization response.

This is the same native catalog exposed by the SDK's get_server_info(). No
prompt, resume, or model request is sent. A short-lived, customization-disabled
child preserves native account/model settings without running hooks or MCPs.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import time

from cc_remote.log import logger
from cc_remote.protocol import MAX_AUTO_COMPACT_TOKENS, MAX_SAFE_WIRE_INTEGER
from cc_remote.wrapper.child_env import claude_profile_process_env
from cc_remote.wrapper.claude_controls import valid_claude_model
from cc_remote.wrapper.claude_runtime import resolve_claude_cli
from cc_remote.workspaces import _claude_runtime_settings

log = logger("cc_remote.wrapper.claude_models")
_TTL = 60.0
_TIMEOUT = 20.0
_MAX_SCOPES = 64
_cache: OrderedDict[tuple, tuple[float, list[dict]]] = OrderedDict()
_lock = asyncio.Lock()
_context_cache: OrderedDict[tuple, tuple[float, int]] = OrderedDict()
_context_lock = asyncio.Lock()


def _normalize(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    models: dict[str, dict] = {}
    for item in raw[:256]:
        if not isinstance(item, dict):
            continue
        # Keep Default as a native selection: availableModels can permit the
        # tier default while forbidding an explicit selection of its resolved
        # ID. Named rows use resolved IDs to expose newly shipped versions.
        value = valid_claude_model(item.get("value"))
        identity = ("default" if value == "default" else
                    valid_claude_model(item.get("resolvedModel")) or value)
        if not identity:
            continue
        levels = item.get("supportedEffortLevels")
        levels = levels if isinstance(levels, list) else []
        efforts = list(dict.fromkeys(
            level for level in levels[:16]
            if isinstance(level, str) and 0 < len(level) <= 64
        )) if item.get("supportsEffort") is not False else []
        is_default = item.get("value") == "default"
        previous = models.get(identity)
        # Repeated rows must not create duplicate choices.
        if previous and is_default:
            previous["is_default"] = True
            continue
        models[identity] = {
            "id": identity,
            "display_name": str(item.get("displayName") or identity)[:4096],
            "description": str(item.get("description") or "")[:4096],
            "efforts": efforts,
            "default_effort": None,
            "is_default": is_default or bool(previous and previous["is_default"]),
        }
    return list(models.values())


async def _read_catalog(
    binary: str, cwd: str, config_dir: str | None, isolate_account_env: bool,
    work_only: bool,
) -> list[dict]:
    env = claude_profile_process_env(
        config_dir, isolate_account_env=isolate_account_env)
    env.pop("CLAUDECODE", None)
    args = [
        binary, "--print", "--input-format", "stream-json",
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence", "--safe-mode", "--strict-mcp-config",
        "--tools", "",
    ]
    with ExitStack() as scope:
        if work_only:
            # Match Work's sole policy source and provider allowlist. Never load
            # user/project customizations or put provider credentials in argv.
            settings = scope.enter_context(tempfile.NamedTemporaryFile(
                mode="w+", prefix="cc-remote-models-", suffix=".json"))
            json.dump(_claude_runtime_settings(config_dir), settings)
            settings.flush()
            args.extend(["--setting-sources", "", "--settings", settings.name])
        elif isolate_account_env:
            args.extend(["--setting-sources", "user"])
        return await _initialize_catalog(args, cwd, env)


async def _initialize_catalog(args: list[str], cwd: str, env: dict[str, str]) -> list[dict]:
    result = await _native_control(args, cwd, env)
    return _normalize(result.get("models"))


async def _native_control(args: list[str], cwd: str, env: dict[str, str],
                          request: dict | None = None) -> dict:
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, cwd=cwd, env=env,
        limit=4 * 1024 * 1024,
    )
    try:
        async with asyncio.timeout(_TIMEOUT):
            proc.stdin.write((json.dumps({
                "type": "control_request", "request_id": "model-catalog",
                "request": {"subtype": "initialize"},
            }) + "\n").encode())
            await proc.stdin.drain()
            while line := await proc.stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    continue
                response = message.get("response")
                if (message.get("type") != "control_response"
                        or not isinstance(response, dict)
                        or response.get("request_id") != "model-catalog"):
                    continue
                if response.get("subtype") != "success":
                    raise RuntimeError("Claude initialization rejected")
                result = response.get("response")
                if request is not None:
                    proc.stdin.write((json.dumps({
                        "type": "control_request", "request_id": "model-context",
                        "request": request,
                    }) + "\n").encode())
                    await proc.stdin.drain()
                    break
                return result if isinstance(result, dict) else {}
            if request is not None:
                while line := await proc.stdout.readline():
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        continue
                    response = message.get("response")
                    if (message.get("type") != "control_response"
                            or not isinstance(response, dict)
                            or response.get("request_id") != "model-context"):
                        continue
                    if response.get("subtype") != "success":
                        raise RuntimeError("Claude context inspection rejected")
                    result = response.get("response")
                    return result if isinstance(result, dict) else {}
            raise RuntimeError("Claude closed before model discovery")
    finally:
        proc.stdin.close()
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), 3.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


async def claude_context_window(*, claude_bin: str, model: str, cwd: str,
                                config_dir: str | None = None,
                                isolate_account_env: bool = False,
                                work_only: bool = False) -> int | None:
    """Ask the native CLI for this model's largest configurable window.

    Live rawMaxTokens includes --autocompact, so a reduced live window cannot
    reveal its ceiling. An empty, non-persisted child requests the largest
    supported CLI window; Claude clamps it to the model/provider's own limit.
    Only initialize and a local summary are sent. No model turn is created.
    """
    if valid_claude_model(model) is None:
        return None
    async with _context_lock:
        try:
            binary, _ = resolve_claude_cli(
                claude_bin or str(Path.home() / ".local/bin/claude"))
            binary = os.path.realpath(binary)
            stat = os.stat(binary)
            target = os.path.realpath(cwd)
            key = (binary, stat.st_mtime_ns, stat.st_size, target, model,
                   os.path.realpath(config_dir) if config_dir else None,
                   isolate_account_env, work_only)
            cached_at, cached = _context_cache.get(key, (0.0, None))
            if cached is not None and time.monotonic() - cached_at < _TTL:
                _context_cache.move_to_end(key)
                return cached
            env = claude_profile_process_env(config_dir, isolate_account_env=isolate_account_env)
            env.pop("CLAUDECODE", None)
            args = [binary, "--print", "--input-format", "stream-json",
                    "--output-format", "stream-json", "--verbose",
                    "--no-session-persistence", "--safe-mode", "--strict-mcp-config",
                    "--tools", "", "--model", model,
                    "--autocompact", str(MAX_AUTO_COMPACT_TOKENS)]
            with ExitStack() as scope:
                if work_only:
                    settings = scope.enter_context(tempfile.NamedTemporaryFile(
                        mode="w+", prefix="cc-remote-context-", suffix=".json"))
                    json.dump(_claude_runtime_settings(config_dir), settings)
                    settings.flush()
                    args.extend(["--setting-sources", "", "--settings", settings.name])
                elif isolate_account_env:
                    args.extend(["--setting-sources", "user"])
                result = await _native_control(args, target, env, {
                    "subtype": "get_context_usage", "detail": "summary",
                })
            maximum = result.get("rawMaxTokens")
            if type(maximum) is not int or not 0 < maximum <= MAX_SAFE_WIRE_INTEGER:
                return None
        except Exception as exc:
            log.warning("Claude context ceiling unavailable", reason=type(exc).__name__)
            return None
        _context_cache[key] = (time.monotonic(), maximum)
        _context_cache.move_to_end(key)
        while len(_context_cache) > _MAX_SCOPES:
            _context_cache.popitem(last=False)
        return maximum


async def claude_model_catalog(
    *, claude_bin: str, cwd: str | None = None,
    config_dir: str | None = None, isolate_account_env: bool = False,
) -> list[dict]:
    """Bounded account/cwd cache; CLI replacement bypasses the TTL immediately.

    Missing cwd means Work discovery using its filtered runtime settings, never
    borrowing customizations from the Wrapper's Code project or user settings.
    """
    async with _lock:
        try:
            binary, _ = resolve_claude_cli(
                claude_bin or str(Path.home() / ".local/bin/claude"))
            binary = os.path.realpath(binary)
            stat = os.stat(binary)
            target = os.path.realpath(os.path.expanduser(cwd or str(Path.home())))
            key = (
                binary, stat.st_mtime_ns, stat.st_size, target,
                os.path.realpath(config_dir) if config_dir else None,
                isolate_account_env, cwd is None,
            )
        except (OSError, RuntimeError):
            return []
        cached_at, cached = _cache.get(key, (0.0, []))
        if key in _cache and time.monotonic() - cached_at < _TTL:
            _cache.move_to_end(key)
            return cached
        try:
            models = await _read_catalog(
                binary, target, config_dir, isolate_account_env, cwd is None)
        except Exception as exc:
            # Native errors can contain provider/account information; report
            # only their class, never dump raw initialization or stderr.
            log.warning("Claude model discovery failed", reason=type(exc).__name__)
            models = []
        result = models or cached
        _cache[key] = (time.monotonic(), result)
        _cache.move_to_end(key)
        while len(_cache) > _MAX_SCOPES:
            _cache.popitem(last=False)
        return result
