"""Refresh an already-enabled task MCP without restarting the native daemon."""
from __future__ import annotations

from pathlib import Path
import tomllib

from cc_remote import task_rpc
from cc_remote.task_store import thread_id

TASK_SERVER = "cc_remote_tasks"
TASK_TOOLS = {"task_start", "task_status", "task_result", "task_cancel"}


def configured_server(config, home):
    entry = config.get("mcp_servers", {}).get(TASK_SERVER)
    if not isinstance(entry, dict) or entry.get("enabled") is False:
        raise ValueError("cc_remote_tasks must already be enabled; configuration left unchanged")
    command = entry.get("command")
    if (not isinstance(command, str) or not Path(command).is_absolute()
            or Path(command).name != "cc-remote"
            or entry.get("args") != ["tasks", "mcp", "--codex-home", str(home)]
            or entry.get("environment_id", "local") != "local"):
        raise ValueError("Custom task MCP launcher; automatic refresh is not supported")
    return entry


async def refresh(home: Path, release: Path, sid: str | None = None):
    """Bind the standard launcher to this immutable generation and verify a call.

    Codex can reuse an unchanged MCP configuration across config reloads. The
    stable cc-remote launcher follows current only when a new process starts;
    an explicit generation cwd makes an upgrade a real native config change.
    This command never enables a missing/disabled server or rewrites its argv.
    """
    home, release = home.expanduser().resolve(strict=True), release.resolve(strict=True)
    if sid is not None:
        thread_id(sid)
    if not all((release / "cc_remote" / name).is_file()
               for name in ("async_tasks.py", "task_store.py", "task_rpc.py")):
        raise ValueError("Task MCP implementation is missing from the selected release")
    path = home / "config.toml"
    local = configured_server(tomllib.loads(path.read_text()), home)
    changed = local.get("cwd") != str(release)
    async with task_rpc.connect(home) as native:
        params = {"includeLayers": True}
        if sid is not None:
            thread = await native.thread(sid)
            cwd = thread.get("cwd")
            if isinstance(cwd, str) and Path(cwd).is_absolute():
                params["cwd"] = cwd
        cfg = await native.rpc("config/read", params)
        effective = configured_server(cfg.get("config", {}), home)
        # A project/profile override must not silently redirect this repair.
        if any(effective.get(key) != local.get(key) for key in ("command", "args", "cwd")):
            raise ValueError("Task MCP configuration is overridden; configuration left unchanged")
        layers = [layer for layer in cfg.get("layers") or []
                  if layer.get("name", {}).get("type") == "user"
                  and layer["name"].get("file") == str(path)
                  and layer["name"].get("profile") is None]
        if len(layers) != 1 or not isinstance(layers[0].get("version"), str):
            raise ValueError("Cannot verify the native user configuration version")
        if changed:
            await native.rpc("config/batchWrite", {
                "filePath": str(path), "expectedVersion": layers[0]["version"],
                # Refresh only MCP below, not unrelated runtime settings.
                "reloadUserConfig": False,
                "edits": [{"keyPath": f"mcp_servers.{TASK_SERVER}.cwd",
                           "value": str(release), "mergeStrategy": "replace"}],
            })
        await native.rpc("config/mcpServer/reload", None)
        report = {"release": str(release), "configuration_changed": changed,
                  "refresh_requested": True, "thread_verified": False}
        # Global tool discovery creates its own connection. It cannot establish
        # that an existing thread stopped using a stale MCP process.
        if sid is not None:
            status = await native.rpc("mcpServerStatus/list", {
                "threadId": sid, "serverName": TASK_SERVER, "detail": "toolsAndAuthOnly",
            }, timeout=45)
            servers = [s for s in status.get("data", []) if s.get("name") == TASK_SERVER]
            if len(servers) != 1 or not TASK_TOOLS.issubset(servers[0].get("tools", {})):
                raise ValueError("Task MCP refresh is unverified: thread tools are unavailable")
            result = await native.rpc("mcpServer/tool/call", {
                "threadId": sid, "server": TASK_SERVER, "tool": "task_status", "arguments": {},
            })
            data = result.get("structuredContent")
            if (result.get("isError") or not isinstance(data, dict)
                    or not isinstance(data.get("tasks"), list)):
                raise ValueError("Task MCP refresh is unverified: the thread's task_status call failed")
            report.update(thread_verified=True, task_count=len(data["tasks"]))
        return report
