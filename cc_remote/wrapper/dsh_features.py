"""Bounded DSH product reads and explicitly addressed subagent controls."""
from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
import tempfile
import time

from cc_remote.protocol import DshCommandResult, DshDownloadChunk, DshItem, DshReadResult
from cc_remote.wrapper.dsh_client import DshError, TESTED_DSH_VERSION, native_session_id, wire_session_id
from cc_remote.wrapper.dsh_stream import text_content

EXPORT_LIMIT = 128 * 1024 * 1024
CHUNK_SIZE = 256 * 1024


class DshFeatures:
    def __init__(self, runtime):
        self.runtime = runtime
        self.exports: dict[str, tuple[str, str, str, int, float]] = {}
        self.export_lock = asyncio.Lock()
        self.inflight = {}
        self.cleanup = None
        self.closed = False

    @property
    def client(self):
        return self.runtime.client

    async def read(self, cmd):
        response = DshReadResult(sid=cmd.sid, to=cmd.client_id,
                                 request_id=cmd.cmd_id, kind=cmd.kind)
        try:
            native = native_session_id(cmd.sid)
            if cmd.target_sid:
                native_session_id(cmd.target_sid)
            if cmd.kind == "search":
                result = await self.client.rpc("session/search", {"request": {"query": cmd.query}})
                sessions = {s["sessionId"]: s for s in await self.client.list_sessions()}
                response.items = [DshItem(id=row["sessionId"], sid=wire_session_id(row["sessionId"]),
                    title=str(sessions.get(row["sessionId"], {}).get("projections", {}).get("values", {}).get("title") or row["sessionId"])[:2048],
                    detail=row.get("snippet", "")[:65536]) for row in result.get("items", [])[:20]]
                response.has_more = bool(result.get("hasMore"))
            elif cmd.kind == "references":
                # Session references are cold reads. File completions use the
                # session's native resolver and its own cwd boundary.
                values = await asyncio.gather(
                    self.client.rpc("fileReferences/list", {"agentId": native, "query": cmd.query}),
                    self.client.rpc("sessionReferenceResolver/candidates", {"agentId": native, "query": cmd.query}),
                    return_exceptions=True)
                failures = []
                for index, value in enumerate(values):
                    if isinstance(value, Exception):
                        failures.append(str(value))
                        continue
                    for row in value[:40]:
                        if index == 0:
                            path = row["path"]
                            response.items.append(DshItem(id="file-" + str(len(response.items)), title=path[:2048],
                                path=path, state=row["kind"], mention=path_mention(path, row["kind"] == "directory")))
                        else:
                            response.items.append(DshItem(id=row["sessionId"], title=row["label"][:2048],
                                sid=wire_session_id(row["sessionId"]), detail=row.get("cwd", "")[:4096],
                                state="session", mention=row["mention"]))
                if failures:
                    response.error = "部分引用来源不可用；可重试。" if response.items else "引用暂不可用，请检查 DSH 的引用插件。"
            elif cmd.kind == "subagents":
                parent = cmd.target_sid or cmd.sid
                if parent != cmd.sid:
                    await self.descendant(cmd.sid, parent)
                result = await self.client.rpc("subagents/list", {"parentSessionId": native_session_id(parent)})
                response.items = [DshItem(id=row["id"], sid=wire_session_id(row["id"]),
                    title=str(row.get("label") or row["id"])[:2048],
                    state=row.get("activity", row.get("reason", "unavailable")),
                    mode=row.get("mode"), has_children=bool(row.get("hasChildren")),
                    controllable=bool(result.get("parentAvailable")) and row.get("mode") == "continuable",
                    detail="父会话未运行；当前仅可查看" if not result.get("parentAvailable") else "")
                    for row in result.get("entries", [])[:256]]
            elif cmd.kind in {"conversation", "deliverables"}:
                target = cmd.target_sid or cmd.sid
                if target != cmd.sid:
                    # Search readers may address ordinary sessions returned by
                    # the device catalog. No read promotes them into Agents.
                    catalog = await self.client.list_sessions()
                    if native_session_id(target) not in {s["sessionId"] for s in catalog}:
                        raise DshError("not_found", "会话不在当前设备的目录中。")
                snapshot = await self.client.history_snapshot(target, before_seq=cmd.before_seq,
                    max_messages=40, query=cmd.query or None, deliverables=cmd.kind == "deliverables")
                records = snapshot["records"]
                if cmd.kind == "deliverables":
                    response.items = [DshItem(id=str(index), title=os.path.basename(row["path"]),
                        path=row["path"], detail=row.get("label", "")[:4096])
                        for index, row in enumerate(snapshot.get("deliverables", [])[:256])]
                else:
                    for record in records:
                        event = record["event"]
                        data = event.get("data", {})
                        if event["type"] not in {"user/message", "assistant/message"} or isinstance(event.get("surfaceOp"), dict):
                            continue
                        message = data.get("message", data)
                        if event["type"] == "user/message" and message.get("source", data.get("source", {})).get("kind") != "user":
                            continue
                        value = text_content(message.get("content", []))
                        if value:
                            response.items.append(DshItem(id=str(event["seq"]), title="你" if event["type"] == "user/message" else "DSH",
                                detail=value[:65536], state="message"))
                    response.has_more = bool(snapshot.get("hasMore"))
                    response.next_seq = records[0]["event"]["seq"] if records and response.has_more else None
            elif cmd.kind == "diagnostics":
                snapshot = await self.client.history_snapshot(cmd.sid, max_messages=1)
                values = snapshot.get("projections", {}).get("values", {})
                response.items = [DshItem(id="connection", title="本机连接", detail="已连接 · 原生控制接口", state="active"),
                    DshItem(id="version", title="DSH 版本", detail=snapshot.get("version") or "无法读取实际版本",
                            state="active" if snapshot.get("version") == TESTED_DSH_VERSION else "unknown"),
                    DshItem(id="adapter", title="适配版本", detail=TESTED_DSH_VERSION),
                    DshItem(id="preset", title="当前预设", detail=str(values.get("agentPreset") or "未提供"))]
                inventory = await self.client.rpc("pluginInventory/list")
                for row in inventory.get("entries", [])[:200]:
                    # Do not forward plugin configuration, expressions, auth,
                    # or raw failure chains, only public inventory metadata.
                    response.items.append(DshItem(id=str(row["entryId"])[:256], title=str(row["moduleName"])[:2048],
                        state=str(row.get("fiberPhase") or "inactive"), detail="已启用" if row.get("enabled") else "未启用"))
                for preset in inventory.get("agentPresets", [])[:30]:
                    response.items.append(DshItem(id="preset-" + preset["id"], title=str(preset.get("name") or preset["id"])[:2048],
                        state="failed" if preset.get("broken") else "active", detail="预设配置不可用" if preset.get("broken") else "预设可用"))
        except DshError as exc:
            response.error, response.available = str(exc)[:512], False
        except (KeyError, TypeError, ValueError):
            response.error, response.available = "DSH 返回的数据不完整，请重试。", False
        await self.runtime.machine.transport.send(response)
        return response

    async def descendant(self, ancestor: str, child: str):
        rows = {s["sessionId"]: s for s in await self.client.list_sessions()}
        cursor = native_session_id(child)
        for _ in range(32):
            row = rows.get(cursor, {})
            if row.get("origin") != "subagent" or not row.get("parentSessionId"):
                break
            cursor = row["parentSessionId"]
            if cursor == native_session_id(ancestor):
                return
        raise DshError("unauthorized", "该子代理不属于当前会话。")

    async def act(self, cmd):
        result = DshCommandResult(sid=cmd.sid, to=cmd.client_id, request_id=cmd.cmd_id, status="success")
        try:
            await self.runtime.ensure_unarchived(cmd.sid)
            await self.runtime.ensure_unarchived(cmd.target_sid)
            catalog = await self.client.rpc("subagents/list", {"parentSessionId": native_session_id(cmd.sid)})
            child = next((x for x in catalog.get("entries", []) if x["id"] == native_session_id(cmd.target_sid)), None)
            if not child or child.get("mode") != "continuable":
                raise DshError("not_resumable", "该子代理当前只支持查看。")
            address = {"parentSessionId": native_session_id(cmd.sid), "childSessionId": native_session_id(cmd.target_sid), "mode": "continuable"}
            if cmd.action == "stop":
                receipt = await self.client.rpc("subagents/interruptByParent", address)
                if not isinstance(receipt, dict) or receipt.get("accepted") is not True:
                    raise DshError("invalid_receipt", "停止请求的结果尚未确认，请刷新后检查。", outcome_unknown=True)
                result.text = "已请求停止子代理"
            else:
                if not cmd.prompt.strip():
                    raise DshError("invalid_prompt", "请输入要发送给子代理的内容。")
                receipt = await self.client.rpc("subagents/prompt", {"request": {**address, "requestId": cmd.cmd_id,
                    "delivery": cmd.action, "content": [{"type": "text", "text": cmd.prompt}]}})
                if not isinstance(receipt, dict) or not isinstance(receipt.get("messageId"), str):
                    raise DshError("invalid_receipt", "消息提交结果尚未确认，请刷新后检查。", outcome_unknown=True)
                result.text = "已引导子代理" if cmd.action == "steer" else "已加入子代理队列"
        except DshError as exc:
            result.status = "unknown" if exc.outcome_unknown else "error"
            result.text = str(exc)[:512]
        except (KeyError, TypeError, ValueError):
            result.status, result.text = "error", "DSH 返回的数据不完整，请刷新后重试。"
        await self.runtime.machine.transport.send(result)
        return result

    async def download(self, cmd):
        response = DshDownloadChunk(sid=cmd.sid, to=cmd.client_id, request_id=cmd.cmd_id, offset=cmd.offset)
        owner = (cmd.sid, cmd.client_id)
        if cmd.cancel:
            pending = self.inflight.get(cmd.export_id)
            if pending and pending[:2] == owner:
                pending[2].cancel()
            row = self.exports.get(cmd.export_id)
            if row and row[:2] == owner:
                self.remove(cmd.export_id)
            response.done = True
            await self.runtime.machine.transport.send(response)
            return response
        self.inflight[cmd.cmd_id] = (*owner, asyncio.current_task())
        try:
            async with self.export_lock:
                if self.closed:
                    raise DshError("closed", "连接已关闭，请重新导出。")
                self.expire()
                key = cmd.export_id
                if not key:
                    if cmd.offset or len(self.exports) >= 2:
                        raise DshError("export_busy", "已有导出正在下载，请完成或取消后重试。")
                    fd, filename = tempfile.mkstemp(prefix="cc-remote-dsh-export-", suffix=".zip")
                    os.close(fd)
                    try:
                        size = await self.client.export_session(cmd.sid, filename, EXPORT_LIMIT)
                    except BaseException:
                        Path(filename).unlink(missing_ok=True)
                        raise
                    key = cmd.cmd_id
                    self.exports[key] = (cmd.sid, cmd.client_id, filename, size, time.monotonic())
                    if self.cleanup is None or self.cleanup.done():
                        self.cleanup = asyncio.create_task(self.clean_expired())
                row = self.exports.get(key)
                if not row or row[:2] != (cmd.sid, cmd.client_id):
                    raise DshError("export_expired", "导出已失效，请重新导出。")
                if cmd.offset > row[3] or cmd.offset % CHUNK_SIZE:
                    raise DshError("export_offset", "下载位置无效，请重新导出。")
                with open(row[2], "rb") as source:
                    source.seek(cmd.offset)
                    chunk = source.read(CHUNK_SIZE)
                self.exports[key] = (*row[:4], time.monotonic())
                response.export_id, response.total = key, row[3]
                response.data = base64.b64encode(chunk).decode("ascii")
                response.done = cmd.offset + len(chunk) >= row[3]
        except DshError as exc:
            response.error = str(exc)[:512]
        except OSError:
            response.error = "无法读取导出文件，请重试。"
        except asyncio.CancelledError:
            response.error = "导出已取消"
        finally:
            self.inflight.pop(cmd.cmd_id, None)
        await self.runtime.machine.transport.send(response)
        return response

    def remove(self, key):
        row = self.exports.pop(key, None)
        if row:
            Path(row[2]).unlink(missing_ok=True)

    def expire(self):
        for key, row in list(self.exports.items()):
            if time.monotonic() - row[4] > 300:
                self.remove(key)

    async def clean_expired(self):
        while self.exports:
            await asyncio.sleep(30)
            self.expire()

    async def close(self):
        self.closed = True
        tasks = [entry[2] for entry in self.inflight.values()]
        if self.cleanup is not None:
            tasks.append(self.cleanup)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for key in list(self.exports):
            self.remove(key)


def path_mention(path: str, directory: bool = False) -> str | None:
    # Match the official file-reference grammar; quotes/control characters
    # cannot be escaped by that grammar and must not become selectable tokens.
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 or c == '"' for c in path):
        return None
    value = path.rstrip("/") + "/" if directory else path
    if any(c.isspace() for c in value):
        return '@"' + value + ('' if directory else '"')
    return '@' + value
