"""Bounded, display-safe discovery of real engine extensions.

Codex exposes first-class app-server inventory RPCs. Claude currently exposes
plugins through its CLI and skills as local manifests, so discovery is read-only
and deliberately avoids importing settings, credentials, schemas or plugin code.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path
from typing import Any
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

from cc_remote.claude_paths import claude_config_dir
from cc_remote.wrapper.child_env import sanitized_child_env
from cc_remote.wrapper.claude_runtime import resolve_claude_cli
from cc_remote.wrapper.codex_rpc import (
    CodexRpcOutcomeUnknown,
    codex_rpc,
    codex_rpc_batch,
)
from cc_remote.wrapper.file_lock_compat import flock, LOCK_EX, LOCK_UN
from cc_remote.wrapper.os_compat import fchmod, fsync_directory

_COMPONENT_TIMEOUT = 8.0
_MAX_ITEMS = 500
_MAX_MANIFEST_BYTES = 128 * 1024
_MAX_SETTINGS_BYTES = 1024 * 1024
_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CLAUDE_HOOK_EVENTS = frozenset({
    "PreToolUse", "PermissionRequest", "PostToolUse", "PostToolUseFailure",
    "Notification", "UserPromptSubmit", "SessionStart", "SessionEnd", "Stop",
    "SubagentStart", "SubagentStop", "PreCompact", "PostCompact",
})


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _source_kind(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return _text(value.get("type"), 256)


def _opaque_id(kind: str, *parts: object) -> str:
    material = "\0".join(str(part) for part in parts)
    return f"{kind}:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _skill_roots(engine: str, cwd: str) -> dict[str, tuple[Path, ...]]:
    project = Path(cwd)
    if engine == "codex":
        home = Path.home()
        return {
            "user": (home / ".codex" / "skills",),
            "project": (
                project / ".codex" / "skills",
                project / ".agents" / "skills",
            ),
        }
    return {
        "user": (claude_config_dir() / "skills",),
        "project": (project / ".claude" / "skills",),
    }


def _skill_containment_base(engine: str, scope: str, cwd: str) -> Path:
    if scope == "user":
        return claude_config_dir() if engine == "claude" else Path.home()
    return Path(cwd)


def _skill_scope(path: Path, engine: str, cwd: str) -> str | None:
    resolved = path.resolve(strict=False)
    for scope, roots in _skill_roots(engine, cwd).items():
        base = _skill_containment_base(
            engine, scope, cwd
        ).resolve(strict=False)
        for root in roots:
            resolved_root = root.resolve(strict=False)
            if (_inside(resolved_root, base)
                    and _inside(resolved, resolved_root)):
                return scope
    return None


def _public_url(value: Any) -> str | None:
    value = _text(value, 4096)
    if not value:
        return None
    parsed = urlsplit(value)
    return value if parsed.scheme == "https" and parsed.hostname else None


async def _codex_component(method: str, params: dict[str, Any], cwd: str):
    return await asyncio.wait_for(
        codex_rpc(method, params, cwd=cwd), timeout=_COMPONENT_TIMEOUT)


async def _codex_components(
    requests: list[tuple[str, dict[str, Any]]],
    cwd: str,
) -> list[Any | Exception]:
    try:
        return await codex_rpc_batch(
            requests, cwd=cwd, timeout=_COMPONENT_TIMEOUT,
        )
    except Exception as exc:
        return [exc for _request in requests]


async def codex_capabilities(
    cwd: str,
    space: str,
    *,
    skills_only: bool = False,
) -> tuple[list[dict], list[str], list[str]]:
    requests = {
        "skills": ("skills/list", {"cwds": [cwd], "forceReload": False}),
        "hooks": ("hooks/list", {"cwds": [cwd]}),
        "plugins": ("plugin/list", {"cwds": [cwd]}),
        "apps": ("app/list", {"limit": _MAX_ITEMS}),
        "mcp": ("mcpServerStatus/list", {}),
    }
    if skills_only:
        requests = {"skills": requests["skills"]}
    results = await _codex_components(list(requests.values()), cwd)
    values = dict(zip(requests, results))
    items: list[dict] = []
    errors: list[str] = []
    notes: list[str] = []

    raw_skills = values["skills"]
    if isinstance(raw_skills, Exception):
        errors.append("skills: app-server request failed")
    elif isinstance(raw_skills, dict):
        for entry in raw_skills.get("data") or []:
            if not isinstance(entry, dict):
                continue
            for skill in entry.get("skills") or []:
                if not isinstance(skill, dict) or len(items) >= _MAX_ITEMS:
                    break
                name = _text(skill.get("name"), 512)
                if not name:
                    continue
                interface = skill.get("interface") if isinstance(skill.get("interface"), dict) else {}
                path = _text(skill.get("path"), 4096)
                if not path:
                    continue
                scope = _text(skill.get("scope"), 256)
                actions: list[str] = []
                if space == "code":
                    actions.append("disable" if bool(skill.get("enabled")) else "enable")
                    if scope in {"user", "repo"} and _skill_scope(
                        Path(path), "codex", cwd
                    ) is not None:
                        actions.append("remove")
                items.append({
                    "kind": "skill", "id": _opaque_id("skill", path), "name": name,
                    "description": (_text(interface.get("shortDescription"), 16 * 1024)
                                    or _text(skill.get("shortDescription"), 16 * 1024)
                                    or _text(skill.get("description"), 16 * 1024)),
                    "enabled": bool(skill.get("enabled")),
                    "scope": scope,
                    "actions": actions,
                })

    raw_hooks = values.get("hooks")
    if isinstance(raw_hooks, Exception):
        errors.append("hooks: app-server request failed")
    elif isinstance(raw_hooks, dict):
        for entry in raw_hooks.get("data") or []:
            if not isinstance(entry, dict):
                continue
            for hook in entry.get("hooks") or []:
                if not isinstance(hook, dict) or len(items) >= _MAX_ITEMS * 2:
                    break
                key = _text(hook.get("key"), 4096)
                event = _text(hook.get("eventName"), 128)
                if not key or not event:
                    continue
                source = _text(hook.get("source"), 256)
                trust = _text(hook.get("trustStatus"), 256)
                matcher = _text(hook.get("matcher"), 2048)
                items.append({
                    "kind": "hook",
                    "id": _opaque_id("hook", key),
                    "name": event,
                    "description": "Codex app-server 原生 Hook",
                    "enabled": bool(hook.get("enabled")),
                    "status": trust,
                    "scope": source,
                    "source": "codex-app-server",
                    "event": event,
                    "matcher": matcher,
                    "handler_type": _text(hook.get("handlerType"), 128),
                    "detail": _text(hook.get("statusMessage"), 4096),
                    "actions": [],
                })
            for warning in entry.get("warnings") or []:
                warning = _text(warning, 512)
                if warning and len(notes) < 32:
                    notes.append(f"Hook: {warning}")

    raw_plugins = values.get("plugins")
    if isinstance(raw_plugins, Exception):
        errors.append("plugins: app-server request failed")
    elif isinstance(raw_plugins, dict):
        for market in raw_plugins.get("marketplaces") or []:
            if not isinstance(market, dict):
                continue
            market_name = _text(market.get("name"), 256)
            for plugin in market.get("plugins") or []:
                if not isinstance(plugin, dict) or len(items) >= _MAX_ITEMS * 2:
                    break
                plugin_id = _text(plugin.get("id"), 512)
                name = _text(plugin.get("name"), 512) or plugin_id
                if not plugin_id or not name:
                    continue
                interface = plugin.get("interface") if isinstance(plugin.get("interface"), dict) else {}
                items.append({
                    "kind": "plugin", "id": plugin_id, "name": name,
                    "description": (_text(interface.get("shortDescription"), 16 * 1024)
                                    or _text(interface.get("longDescription"), 16 * 1024)),
                    "enabled": bool(plugin.get("enabled")),
                    "installed": bool(plugin.get("installed")),
                    "status": _text(plugin.get("availability"), 256),
                    "scope": market_name,
                    "source": _source_kind(plugin.get("source")),
                    "actions": ([] if space == "work" else [
                        "uninstall" if bool(plugin.get("installed")) else "install"
                    ]),
                })

    raw_apps = values.get("apps")
    if isinstance(raw_apps, Exception):
        errors.append("apps: app-server request failed")
    elif isinstance(raw_apps, dict):
        for app in (raw_apps.get("data") or [])[:_MAX_ITEMS]:
            if not isinstance(app, dict):
                continue
            app_id = _text(app.get("id"), 512)
            name = _text(app.get("name"), 512) or app_id
            if not app_id or not name:
                continue
            items.append({
                "kind": "app", "id": app_id, "name": name,
                "description": _text(app.get("description"), 16 * 1024),
                "enabled": bool(app.get("isEnabled", True)),
                "status": "accessible" if app.get("isAccessible") else "link required",
                "source": _text(app.get("distributionChannel"), 256),
                "install_url": _public_url(app.get("installUrl")),
            })

    raw_mcp = values.get("mcp")
    if isinstance(raw_mcp, Exception):
        errors.append("mcp: app-server request failed")
    elif isinstance(raw_mcp, dict):
        for server in (raw_mcp.get("data") or [])[:_MAX_ITEMS]:
            if not isinstance(server, dict):
                continue
            name = _text(server.get("name"), 512)
            if not name:
                continue
            info = server.get("serverInfo") if isinstance(server.get("serverInfo"), dict) else {}
            tools = server.get("tools") if isinstance(server.get("tools"), dict) else {}
            resources = server.get("resources") if isinstance(server.get("resources"), list) else []
            templates = server.get("resourceTemplates") if isinstance(server.get("resourceTemplates"), list) else []
            items.append({
                "kind": "mcp", "id": name, "name": _text(info.get("title"), 512) or name,
                "description": _text(info.get("description"), 16 * 1024),
                "status": _text(server.get("authStatus"), 256),
                "tool_count": len(tools),
                "resource_count": len(resources) + len(templates),
            })

    if space == "work":
        notes.append("Work 中的实际可用范围仍受私有目录、禁网和权限策略限制。")
    return items[:2000], errors[:32], notes


def _manifest_metadata(path: Path) -> tuple[str | None, str | None]:
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_MANIFEST_BYTES:
            return None, None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, None
    if not text.startswith("---"):
        return None, None
    end = text.find("\n---", 3)
    if end < 0:
        return None, None
    frontmatter = text[3:end]
    name = re.search(r"(?m)^name:\s*[\"']?([^\n\"']+)", frontmatter)
    description = re.search(r"(?m)^description:\s*[\"']?([^\n\"']+)", frontmatter)
    return (
        _text(name.group(1), 512) if name else None,
        _text(description.group(1), 16 * 1024) if description else None,
    )


def _claude_skills(cwd: str) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for scope, roots in _skill_roots("claude", cwd).items():
        base = _skill_containment_base(
            "claude", scope, cwd
        ).resolve(strict=False)
        for root in roots:
            if (root.is_symlink()
                    or not _inside(root.resolve(strict=False), base)):
                continue
            try:
                candidates = list(root.iterdir())[:_MAX_ITEMS]
            except OSError:
                continue
            for candidate in candidates:
                manifest = candidate / "SKILL.md"
                name, description = _manifest_metadata(manifest)
                name = name or _text(candidate.name, 512)
                if not name or name in seen:
                    continue
                seen.add(name)
                items.append({
                    "kind": "skill",
                    "id": _opaque_id("skill", candidate.resolve()),
                    "name": name,
                    "description": description,
                    "enabled": True,
                    "scope": scope,
                    "actions": ["remove"],
                })
    return items


def _claude_settings_files(cwd: str) -> tuple[tuple[Path, str], ...]:
    project = Path(cwd) / ".claude"
    return (
        (claude_config_dir() / "settings.json", "user"),
        (project / "settings.json", "project"),
        (project / "settings.local.json", "project-local"),
    )


def _settings_path_safe(path: Path, cwd: str, scope: str) -> bool:
    base = (
        claude_config_dir() if scope == "user" else Path(cwd)
    ).resolve(strict=False)
    try:
        return (not path.parent.is_symlink()
                and _inside(path.parent.resolve(strict=False), base))
    except OSError:
        return False


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or info.st_size > _MAX_SETTINGS_BYTES):
        raise ValueError("Hook 配置不是受支持的常规 JSON 文件")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Hook 配置无法安全读取") from exc
    if not isinstance(raw, dict):
        raise ValueError("Hook 配置顶层必须是对象")
    return raw


def _claude_hook_rows(
    cwd: str,
) -> list[tuple[dict[str, Any], Path, str, str, int, int, str | None]]:
    rows: list[tuple[dict[str, Any], Path, str, str, int, int, str | None]] = []
    for path, scope in _claude_settings_files(cwd):
        if not _settings_path_safe(path, cwd, scope):
            continue
        try:
            settings = _read_json_object(path)
        except ValueError:
            continue
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            continue
        for event, groups in hooks.items():
            if not isinstance(event, str) or not isinstance(groups, list):
                continue
            for group_index, group in enumerate(groups):
                if not isinstance(group, dict):
                    continue
                handlers = group.get("hooks")
                if not isinstance(handlers, list):
                    continue
                matcher = _text(group.get("matcher"), 2048)
                for hook_index, handler in enumerate(handlers):
                    if isinstance(handler, dict):
                        rows.append((
                            handler, path, scope, event, group_index, hook_index,
                            matcher,
                        ))
    return rows


def _claude_hooks(cwd: str) -> list[dict]:
    items: list[dict] = []
    for handler, path, scope, event, group_index, hook_index, matcher in _claude_hook_rows(cwd):
        handler_type = _text(handler.get("type"), 128) or "command"
        command = _text(handler.get("command"), 16 * 1024) or ""
        hook_id = _opaque_id(
            "hook", path.resolve(strict=False), event, group_index, hook_index,
            handler_type, command,
        )
        actions = ["remove"] if handler_type == "command" else []
        items.append({
            "kind": "hook", "id": hook_id, "name": event,
            "description": "Claude 本地 Hook",
            "enabled": True, "scope": scope, "source": "claude-settings",
            "event": event, "matcher": matcher, "handler_type": handler_type,
            "detail": _text(handler.get("statusMessage"), 4096),
            "actions": actions,
        })
    return items[:_MAX_ITEMS]


async def _claude_plugins(binary: str) -> list[dict]:
    proc = await asyncio.create_subprocess_exec(
        binary, "plugin", "list", "--json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=sanitized_child_env(),
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), _COMPONENT_TIMEOUT)
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0 or len(stdout) > 4 * 1024 * 1024:
        raise RuntimeError("claude plugin list failed")
    raw = json.loads(stdout)
    if not isinstance(raw, list):
        raise RuntimeError("claude plugin list returned invalid data")
    items: list[dict] = []
    for plugin in raw[:_MAX_ITEMS]:
        if not isinstance(plugin, dict):
            continue
        plugin_id = _text(plugin.get("id") or plugin.get("name"), 512)
        name = _text(plugin.get("name") or plugin_id, 512)
        if not plugin_id or not name:
            continue
        items.append({
            "kind": "plugin", "id": plugin_id, "name": name,
            "description": _text(plugin.get("description"), 16 * 1024),
            "enabled": bool(plugin.get("enabled", True)), "installed": True,
            "scope": _text(plugin.get("scope"), 256), "source": "claude-cli",
            "actions": ["uninstall"],
        })
    return items


async def _codex_plugin_inventory(cwd: str) -> AsyncIterator[tuple[dict, dict]]:
    raw = await _codex_component("plugin/list", {"cwds": [cwd]}, cwd)
    if not isinstance(raw, dict):
        return
    for marketplace in raw.get("marketplaces") or []:
        if not isinstance(marketplace, dict):
            continue
        for plugin in marketplace.get("plugins") or []:
            if isinstance(plugin, dict):
                yield marketplace, plugin


async def _codex_plugin_state(plugin_id: str, cwd: str):
    async for marketplace, plugin in _codex_plugin_inventory(cwd):
        if _text(plugin.get("id"), 512) == plugin_id:
            return marketplace, plugin
    raise ValueError("Codex 插件不存在或当前目录不可见")


async def _manage_codex_plugin(plugin_id: str, action: str, cwd: str) -> None:
    marketplace, plugin = await _codex_plugin_state(plugin_id, cwd)
    installed = bool(plugin.get("installed"))
    if installed == (action == "install"):
        return
    if action == "uninstall":
        method, params = "plugin/uninstall", {"pluginId": plugin_id}
    else:
        name = _text(plugin.get("name"), 512)
        if not name:
            raise ValueError("Codex 插件缺少安装名称")
        params: dict[str, Any] = {"pluginName": name}
        marketplace_path = _text(marketplace.get("path"), 4096)
        marketplace_name = _text(marketplace.get("name"), 512)
        if marketplace_path:
            params["marketplacePath"] = marketplace_path
        elif marketplace_name:
            params["remoteMarketplaceName"] = marketplace_name
        method = "plugin/install"
    try:
        await codex_rpc(method, params, cwd=cwd)
    except CodexRpcOutcomeUnknown:
        _, current = await _codex_plugin_state(plugin_id, cwd)
        if bool(current.get("installed")) != (action == "install"):
            raise


async def _manage_claude_plugin(
    plugin_id: str, action: str, binary: str
) -> None:
    verb = "install" if action == "install" else "uninstall"
    proc = await asyncio.create_subprocess_exec(
        binary, "plugin", verb, plugin_id,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        env=sanitized_child_env(),
    )
    try:
        await asyncio.wait_for(proc.wait(), 60.0)
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise ValueError(f"Claude 插件{('安装' if action == 'install' else '卸载')}失败")


def _local_skill_path(engine: str, skill_id: str, cwd: str) -> Path:
    for scope, roots in _skill_roots(engine, cwd).items():
        for root in roots:
            base = _skill_containment_base(
                engine, scope, cwd
            ).resolve(strict=False)
            if root.is_symlink() or not _inside(root.resolve(strict=False), base):
                continue
            try:
                candidates = list(root.iterdir())[:_MAX_ITEMS]
            except OSError:
                continue
            resolved_root = root.resolve(strict=False)
            for candidate in candidates:
                try:
                    if candidate.is_symlink() or not candidate.is_dir():
                        continue
                    resolved = candidate.resolve(strict=True)
                except OSError:
                    continue
                if resolved.parent != resolved_root:
                    continue
                if _opaque_id("skill", resolved) == skill_id:
                    return resolved
    raise ValueError("Skill 不存在、已变化或不可管理，请刷新后重试")


async def _codex_skill_path(skill_id: str, cwd: str) -> tuple[Path, str, bool]:
    raw = await _codex_component(
        "skills/list", {"cwds": [cwd], "forceReload": True}, cwd
    )
    if not isinstance(raw, dict):
        raise ValueError("Codex Skill 目录暂不可用")
    for entry in raw.get("data") or []:
        if not isinstance(entry, dict):
            continue
        for skill in entry.get("skills") or []:
            if not isinstance(skill, dict):
                continue
            path_value = _text(skill.get("path"), 4096)
            if not path_value or _opaque_id("skill", path_value) != skill_id:
                continue
            return (
                Path(path_value),
                _text(skill.get("scope"), 256) or "unknown",
                bool(skill.get("enabled")),
            )
    raise ValueError("Codex Skill 不存在、已变化或不可管理，请刷新后重试")


def _create_skill(
    engine: str,
    cwd: str,
    scope: str,
    name: str,
    description: str,
    instructions: str,
) -> None:
    if not _SKILL_NAME.fullmatch(name):
        raise ValueError("Skill 名称仅允许字母、数字、点、下划线和短横线")
    roots = _skill_roots(engine, cwd)[scope]
    root = roots[0] if scope == "user" or engine != "codex" else roots[-1]
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Skill 根目录不安全")
    root = root.resolve(strict=True)
    base = _skill_containment_base(
        engine, scope, cwd
    ).resolve(strict=True)
    if not _inside(root, base):
        raise ValueError("Skill 根目录不能指向用户或项目目录之外")
    target = root / name
    if target.exists() or target.is_symlink():
        raise ValueError("同名 Skill 已存在")
    target.mkdir(mode=0o700)
    try:
        safe_description = description.strip() or f"Instructions for {name}."
        body = (
            "---\n"
            f"name: {json.dumps(name, ensure_ascii=False)}\n"
            f"description: {json.dumps(safe_description, ensure_ascii=False)}\n"
            "---\n\n"
            f"{instructions.strip()}\n"
        )
        temp = target / ".SKILL.md.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target / "SKILL.md")
        finally:
            if temp.exists():
                temp.unlink()
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


def _trash_skill(path: Path, engine: str, cwd: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Skill 目录不安全")
    scope = _skill_scope(path, engine, cwd)
    if scope is None:
        raise ValueError("系统或管理员 Skill 不能删除")
    roots = _skill_roots(engine, cwd)[scope]
    root = next((root.resolve(strict=False) for root in roots
                 if path.resolve(strict=True).parent == root.resolve(strict=False)), None)
    if root is None:
        raise ValueError("只能删除 Skill 根目录的直接子项")
    trash = root.parent / ".cc-remote-trash" / "skills"
    trash.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = trash / f"{path.name}-{int(time.time() * 1000)}-{os.getpid()}"
    os.replace(path, destination)


async def manage_engine_skill(
    engine: str,
    action: str,
    cwd: str,
    *,
    space: str = "code",
    skill_id: str | None = None,
    name: str | None = None,
    description: str = "",
    instructions: str = "",
    scope: str = "user",
) -> None:
    if space == "work":
        raise ValueError("Work 不允许修改 Code 扩展")
    target = os.path.realpath(os.path.expanduser(cwd))
    if not os.path.isdir(target):
        raise ValueError("Skill 工作目录不存在")
    if action == "create":
        if not name or not instructions.strip():
            raise ValueError("创建 Skill 需要名称和说明")
        await asyncio.to_thread(
            _create_skill, engine, target, scope, name,
            description, instructions,
        )
        return
    if not skill_id:
        raise ValueError("缺少 Skill 标识")
    if action in {"enable", "disable"}:
        if engine != "codex":
            raise ValueError("Claude CLI 没有独立的 Skill 启停接口")
        path, _scope, enabled = await _codex_skill_path(skill_id, target)
        desired = action == "enable"
        if enabled != desired:
            await codex_rpc(
                "skills/config/write", {"path": str(path), "enabled": desired},
                cwd=target,
            )
        return
    if action != "remove":
        raise ValueError("不支持的 Skill 操作")
    if engine == "codex":
        path, native_scope, _enabled = await _codex_skill_path(skill_id, target)
        if native_scope not in {"user", "repo"}:
            raise ValueError("系统或管理员 Skill 不能删除")
    else:
        path = await asyncio.to_thread(_local_skill_path, engine, skill_id, target)
    await asyncio.to_thread(_trash_skill, path.resolve(strict=True), engine, target)


def _atomic_update_json(path: Path, mutate) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("Hook 配置路径不能是符号链接")
    lock_path = path.with_name(path.name + ".cc-remote.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(lock_path, flags, 0o600)
    try:
        flock(lock_fd, LOCK_EX)
        current = _read_json_object(path)
        updated = mutate(current)
        if not isinstance(updated, dict):
            raise ValueError("Hook 配置更新无效")
        encoded = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode()
        if len(encoded) > _MAX_SETTINGS_BYTES:
            raise ValueError("Hook 配置超过安全大小限制")
        mode = 0o600
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            pass
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            fchmod(fd, temp_name, mode)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            fsync_directory(path.parent)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    finally:
        flock(lock_fd, LOCK_UN)
        os.close(lock_fd)


def _claude_hook_path(cwd: str, scope: str) -> Path:
    base = (
        claude_config_dir() if scope == "user" else Path(cwd)
    ).resolve(strict=False)
    directory = base if scope == "user" else base / ".claude"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not _inside(directory.resolve(strict=True), base):
        raise ValueError("Hook 配置目录不能指向用户或项目目录之外")
    return directory / ("settings.json" if scope == "user" else "settings.local.json")


def _create_claude_hook(
    cwd: str, scope: str, event: str, matcher: str, command: str, timeout: int,
) -> None:
    if event not in _CLAUDE_HOOK_EVENTS:
        raise ValueError("不支持的 Claude Hook 事件")
    if not command.strip():
        raise ValueError("Hook 命令不能为空")
    path = _claude_hook_path(cwd, scope)

    def mutate(settings: dict[str, Any]) -> dict[str, Any]:
        hooks = settings.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("现有 hooks 字段不是对象")
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError("现有 Hook 事件配置不是列表")
        handler: dict[str, Any] = {"type": "command", "command": command.strip()}
        if timeout:
            handler["timeout"] = timeout
        group: dict[str, Any] = {"hooks": [handler]}
        if matcher.strip():
            group["matcher"] = matcher.strip()
        groups.append(group)
        return settings

    _atomic_update_json(path, mutate)


def _remove_claude_hook(cwd: str, hook_id: str) -> None:
    match = None
    for row in _claude_hook_rows(cwd):
        handler, path, _scope, event, group_index, hook_index, _matcher = row
        command = _text(handler.get("command"), 16 * 1024) or ""
        candidate = _opaque_id(
            "hook", path.resolve(strict=False), event, group_index, hook_index,
            _text(handler.get("type"), 128) or "command", command,
        )
        if candidate == hook_id:
            match = (path, event, group_index, hook_index)
            break
    if match is None:
        raise ValueError("Hook 不存在、已变化或不可管理，请刷新后重试")
    path, event, group_index, hook_index = match

    def mutate(settings: dict[str, Any]) -> dict[str, Any]:
        try:
            groups = settings["hooks"][event]
            handlers = groups[group_index]["hooks"]
            handler = handlers[hook_index]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Hook 已变化，请刷新后重试") from exc
        if not isinstance(handler, dict) or handler.get("type", "command") != "command":
            raise ValueError("只能删除命令 Hook")
        current_id = _opaque_id(
            "hook", path.resolve(strict=False), event, group_index, hook_index,
            _text(handler.get("type"), 128) or "command",
            _text(handler.get("command"), 16 * 1024) or "",
        )
        if current_id != hook_id:
            raise ValueError("Hook 已变化，请刷新后重试")
        del handlers[hook_index]
        if not handlers:
            del groups[group_index]
        if not groups:
            del settings["hooks"][event]
        if not settings.get("hooks"):
            settings.pop("hooks", None)
        return settings

    _atomic_update_json(path, mutate)


async def manage_engine_hook(
    engine: str,
    action: str,
    cwd: str,
    *,
    space: str = "code",
    hook_id: str | None = None,
    event: str | None = None,
    matcher: str = "",
    command: str = "",
    timeout: int = 60,
    scope: str = "user",
) -> None:
    if space == "work":
        raise ValueError("Work 不允许修改 Code 扩展")
    if engine != "claude":
        raise ValueError("Codex app-server 当前只提供 Hook 清单，没有写接口")
    target = os.path.realpath(os.path.expanduser(cwd))
    if not os.path.isdir(target):
        raise ValueError("Hook 工作目录不存在")
    if action == "create":
        if not event:
            raise ValueError("缺少 Hook 事件")
        await asyncio.to_thread(
            _create_claude_hook, target, scope, event, matcher, command, timeout,
        )
        return
    if action != "remove" or not hook_id:
        raise ValueError("缺少 Hook 标识")
    await asyncio.to_thread(_remove_claude_hook, target, hook_id)


async def manage_engine_plugin(
    engine: str,
    plugin_id: str,
    action: str,
    cwd: str,
    *,
    space: str = "code",
    claude_bin: str = "",
) -> None:
    if space == "work":
        raise ValueError("Work 不允许修改引擎插件")
    target = os.path.realpath(os.path.expanduser(cwd))
    if not os.path.isdir(target):
        raise ValueError("插件目录不存在")
    if engine == "codex":
        await _manage_codex_plugin(plugin_id, action, target)
    else:
        binary, _ = resolve_claude_cli(claude_bin)
        await _manage_claude_plugin(plugin_id, action, binary)


async def claude_capabilities(
    cwd: str,
    space: str,
    claude_bin: str = "",
    *,
    skills_only: bool = False,
) -> tuple[list[dict], list[str], list[str]]:
    if space == "work":
        return [], [], [
            "Claude Work 为防止 Code 配置泄漏，明确禁用了用户/项目技能、插件、Hook 与 MCP。"
        ]
    items = await asyncio.to_thread(_claude_skills, cwd)
    if skills_only:
        return items[:2000], [], []
    items.extend(await asyncio.to_thread(_claude_hooks, cwd))
    errors: list[str] = []
    try:
        binary, _ = resolve_claude_cli(claude_bin)
        items.extend(await _claude_plugins(binary))
    except Exception:
        errors.append("plugins: claude CLI request failed")
    return items[:2000], errors, []


async def engine_capabilities(
    engine: str,
    cwd: str,
    space: str,
    claude_bin: str = "",
    *,
    skills_only: bool = False,
):
    target = os.path.realpath(os.path.expanduser(cwd))
    if not os.path.isdir(target):
        raise ValueError("capability cwd does not exist")
    if engine == "codex":
        return await codex_capabilities(
            target, space, skills_only=skills_only,
        )
    return await claude_capabilities(
        target, space, claude_bin, skills_only=skills_only,
    )
