"""Local-only client for the DSH 0.1.5 Remote Gateway.

This is a control connection, not a model client. DSH owns its process, model
credentials and session store. In particular, closing a subscription never
calls session/cancel. Mutation requests are never retried after an ambiguous
transport failure.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import stat
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect

TESTED_DSH_VERSION = "0.1.5-rc.2"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_STREAM_ITEMS = 128
MAX_STREAMS = 32
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,123}$")
_COOKIE = re.compile(r"^dsh-auth-[A-Za-z0-9_-]+=v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_RPC_ENDPOINTS = frozenset({
    "session/list", "session/modelCatalog", "session/create",
    "session/selectModel", "session/prompt", "session/cancel",
    "session/page", "session/attachment", "session/updateQueue",
    "session/rename", "session/fork", "agentPresets/list",
    "commands/list", "commands/execute", "skills/list", "$events/result",
    "fileUploads/upload", "goals/get", "session/search", "fileReferences/list",
    "sessionReferenceResolver/candidates", "subagents/list", "subagents/prompt",
    "subagents/interruptByParent", "workspace/archiveSession", "pluginInventory/list",
})
_STREAM_ENDPOINTS = frozenset({"session/follow", "session/control", "workspace/follow", "$events"})


class DshError(RuntimeError):
    """Display-safe failure; never retain a request URL, headers or payload."""

    def __init__(self, code: str, message: str, *, outcome_unknown: bool = False):
        super().__init__(message)
        self.code = code
        self.outcome_unknown = outcome_unknown


def native_session_id(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("dsh@"):
        raise DshError("invalid_session", "DSH 会话编号无效。")
    native = value[4:]
    if not _ID.fullmatch(native):
        raise DshError("invalid_session", "DSH 会话编号无效。")
    return native


def wire_session_id(value: str) -> str:
    native_session_id("dsh@" + value)
    return "dsh@" + value


def local_origin(value: str) -> str:
    """Accept a literal loopback authority, never DNS or a proxy destination."""
    try:
        parsed = urlsplit(value)
        host = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
        if (parsed.scheme != "http" or not host.is_loopback
                or (host.version == 6 and (host.ipv4_mapped or host.scope_id))
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or port is None or not 1 <= port <= 65535
                or any(c.isspace() for c in value)):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise DshError(
            "invalid_connection", "DSH 地址须为带端口的本机 IP，例如 http://127.0.0.1:3080。",
        ) from None
    host_text = f"[{host}]" if host.version == 6 else str(host)
    return f"http://{host_text}:{port}"


def _cookie_for_origin(cookie: str, origin: str) -> None:
    """Check public cookie scope and expiry; DSH verifies its signature."""
    try:
        if not isinstance(cookie, str) or len(cookie) > 4096 or not _COOKIE.fullmatch(cookie):
            raise ValueError
        authority = urlsplit(origin).netloc
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(authority.encode()).digest(),
        ).decode().rstrip("=")
        name, value = cookie.split("=", 1)
        if name != "dsh-auth-" + expected:
            raise ValueError
        body = value.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if (not isinstance(payload, dict) or payload.get("version") != 1
                or payload.get("authority") != authority
                or type(payload.get("expiresAt")) is not int):
            raise ValueError
        if payload["expiresAt"] <= time.time() * 1000:
            raise DshError("auth_expired", "DSH 本机配对已过期，请重新配对。")
    except (ValueError, KeyError, IndexError, TypeError):
        raise DshError("invalid_connection", "DSH 本机配对文件无效，请重新配对。") from None


@dataclass(frozen=True)
class DshConnection:
    origin: str
    cookie: str = field(repr=False)

    def __post_init__(self):
        if local_origin(self.origin) != self.origin:
            raise DshError("invalid_connection", "DSH 本机地址不是规范地址。")
        _cookie_for_origin(self.cookie, self.origin)

    @classmethod
    def load(cls, path: Path) -> DshConnection:
        """Read exactly one owner-only file, without following symlinks."""
        try:
            fd = os.open(path.expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_size > 8192):
                    raise ValueError
                raw = stream.read(8193)
            if len(raw) > 8192:
                raise ValueError
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {"origin", "cookie"}:
                raise ValueError
            return cls(data["origin"], data["cookie"])
        except DshError:
            raise
        except (OSError, ValueError, TypeError):
            raise DshError(
                "not_paired", "DSH 尚未配对，或本机配对文件不是仅当前用户可读的普通文件。",
            ) from None


def _remote_error(value: object) -> DshError:
    # The upstream message/details can contain prompt data or plugin secrets.
    # Keep its bounded machine code, and translate only known public failures.
    code = value.get("code") if isinstance(value, dict) else None
    if not isinstance(code, str) or not re.fullmatch(r"[a-zA-Z0-9/_-]{1,100}", code):
        code = "invalid_response"
    if (isinstance(value, dict) and code == "gateway/internal"
            and "session search is disabled" in str(value.get("message", ""))):
        return DshError("search_disabled", "DSH 尚未开启全文搜索，请启用会话索引。")
    messages = {
        "subagent/parent-unavailable": "父会话尚未运行，当前只能查看子代理记录。",
        "subagent/not-resumable": "该子代理当前只支持查看。",
        "subagent/unauthorized": "该子代理不属于当前会话。",
        "subagent/delivery-unavailable": "子代理暂时无法接收消息，请稍后重试。",
        "session/not-found": "DSH 会话不存在，请刷新会话列表。",
        "session/model-unavailable": "所选 DSH 模型当前不可用，请在 DSH 中检查模型配置。",
        "session/agent-busy": "DSH 会话正忙，请稍后重试。",
        "agent-preset/not-found": "所选 DSH Agent Preset 不存在。",
        "session/attachment-invalid": "DSH 拒绝了附件，请检查格式和大小。",
        "gateway/not-found": "DSH 接口不兼容；此适配器适用于 0.1.5。",
    }
    return DshError(code, messages.get(code, f"DSH 请求失败（{code}）。"))


class DshClient:
    def __init__(self, connection: DshConnection, *, http_transport=None):
        self.connection = connection
        self._http = httpx.AsyncClient(
            base_url=connection.origin, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(15, connect=5), transport=http_transport,
            headers={"Cookie": connection.cookie, "Origin": connection.origin},
        )
        self._socket = None
        self._reader: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._streams: dict[str, asyncio.Queue] = {}
        self._queued_bytes = 0
        self._closed = False

    async def list_sessions(self) -> list[dict]:
        # Typert preserves the declared parameter name, including its leading
        # underscore. `{request: {}}` belongs to other Session methods and is
        # rejected by this endpoint's strict named-argument descriptor.
        result = await self.rpc("session/list", {"_request": {}})
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise DshError("invalid_catalog", "DSH 会话列表格式不兼容。")
        items = result["items"]
        for item in items:
            if (not isinstance(item, dict)
                    or not isinstance(item.get("sessionId"), str)
                    or not _ID.fullmatch(item["sessionId"])
                    or type(item.get("running")) is not bool):
                raise DshError("invalid_catalog", "DSH 会话列表格式不兼容。")
        return items

    async def rpc(self, endpoint: str, args: dict | None = None) -> object:
        if self._closed:
            raise DshError("closed", "DSH 连接已关闭。")
        if endpoint not in _RPC_ENDPOINTS:
            raise DshError("unsupported", "此 DSH 操作尚未适配。")
        request_id = str(uuid4())
        envelope = {
            "type": "client-request", "rpcId": request_id,
            "method": endpoint, "payload": {"args": args or {}},
        }
        try:
            async with self._http.stream("POST", "/api/" + endpoint, json=envelope) as response:
                if response.status_code in {401, 403}:
                    raise DshError("auth_required", "DSH 本机认证失败，请重新配对。")
                if response.status_code == 404:
                    raise DshError("gateway/not-found", "DSH 接口不兼容；此适配器适用于 0.1.5。")
                if response.status_code != 200:
                    raise DshError("unavailable", "DSH 未能处理请求。请检查本机 DSH。", outcome_unknown=True)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise DshError("too_large", "DSH 响应过大，请缩小历史页或附件。", outcome_unknown=True)
        except httpx.HTTPError:
            raise DshError(
                "disconnected", "DSH 本机连接中断。已提交操作的结果未知，请先刷新确认。",
                outcome_unknown=True,
            ) from None
        try:
            data = json.loads(raw)
            if (not isinstance(data, dict) or data.get("type") != "server-response"
                    or data.get("rpcId") != request_id or not isinstance(data.get("result"), dict)):
                raise ValueError
            result = data["result"]
            if result.get("ok") is False:
                raise _remote_error(result.get("error"))
            if result.get("ok") is not True:
                raise ValueError
            return result.get("value")
        except (ValueError, TypeError):
            raise DshError("invalid_response", "DSH 返回了不兼容的响应。", outcome_unknown=True) from None

    async def history_snapshot(
        self, session_id: str, *, before_seq: int | None = None,
        through_seq: int | None = None, max_messages: int = 16,
        query: str | None = None, deliverables: bool = False,
    ) -> dict:
        """Obtain an exact cold page via the optional read-only Host plugin.

        Never substitute follow here: cancelling follow after its first frame
        still races the Host's promotion of the cold Session to a live Agent.
        """
        if self._closed:
            raise DshError("closed", "DSH 连接已关闭。")
        native = native_session_id(session_id)
        for value, lower, upper in (
            (max_messages, 1, 100),
            (before_seq, 0, 9_007_199_254_740_991),
            (through_seq, -1, 9_007_199_254_740_991),
        ):
            if value is not None and (type(value) is not int or not lower <= value <= upper):
                raise DshError("invalid_page", "DSH 历史分页参数无效。")
        params = {"sessionId": native, "maxMessages": max_messages}
        if query:
            if len(query) > 1000:
                raise DshError("invalid_query", "搜索内容过长。")
            params["query"] = query
        if deliverables:
            params["deliverables"] = "1"
        if before_seq is not None:
            params["beforeSeq"] = before_seq
        if through_seq is not None:
            params["throughSeq"] = through_seq
        try:
            async with self._http.stream("GET", "/api/cc-remote.snapshot", params=params) as response:
                if response.status_code == 404:
                    raise DshError("history_bridge_required", "DSH 尚未加载 cc-remote 的只读历史插件。")
                if response.status_code in {401, 403}:
                    raise DshError("auth_required", "DSH 本机认证失败，请重新配对。")
                if response.status_code == 409 and query:
                    raise DshError("search_match_changed", "搜索结果已变化，请重新搜索后打开。")
                if response.status_code != 200:
                    raise DshError("history_unavailable", "DSH 历史暂不可读取，请刷新后重试。")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise DshError("too_large", "DSH 历史页过大，请减少每页消息数。")
        except httpx.HTTPError:
            raise DshError("disconnected", "DSH 本机连接中断，未能读取历史。") from None
        try:
            data = json.loads(raw)
            header = data.get("header") if isinstance(data, dict) else None
            if (type(data.get("contract")) is not int or data["contract"] != 1
                    or not isinstance(header, dict)
                    or header.get("version") != 3 or header.get("id") != native
                    or type(data.get("cursor")) is not int
                    or not -1 <= data["cursor"] <= 9_007_199_254_740_991
                    or (through_seq is not None and data["cursor"] != through_seq)
                    or type(data.get("hasMore")) is not bool
                    or not isinstance(data.get("records"), list)):
                raise ValueError
            previous = None
            for record in data["records"]:
                event = record.get("event") if isinstance(record, dict) else None
                if (not isinstance(event, dict) or record.get("type") != "event"
                        or type(event.get("seq")) is not int
                        or not 0 <= event["seq"] <= data["cursor"]
                        or (before_seq is not None and event["seq"] >= before_seq)
                        or (previous is not None and event["seq"] != previous + 1)):
                    raise ValueError
                previous = event["seq"]
            return data
        except (ValueError, TypeError, AttributeError):
            raise DshError("invalid_history", "DSH 历史页身份或序列不一致，请重新读取。") from None

    async def _ensure_socket(self):
        async with self._lock:
            if self._closed:
                raise DshError("closed", "DSH 连接已关闭。")
            if self._socket is not None:
                return self._socket
            try:
                socket = await connect(
                    "ws" + self.connection.origin[4:] + "/api/remote.mux",
                    additional_headers={"Cookie": self.connection.cookie},
                    origin=self.connection.origin, proxy=None,
                    open_timeout=5, close_timeout=2,
                    max_size=MAX_RESPONSE_BYTES, max_queue=16,
                )
            except Exception:
                raise DshError("disconnected", "无法连接 DSH 实时接口，请确认本机服务和配对状态。") from None
            self._socket = socket
            self._reader = asyncio.create_task(self._read(socket))
            return socket

    async def _read(self, socket):
        failure = DshError("disconnected", "DSH 实时连接中断，请刷新会话。")
        try:
            async for raw in socket:
                if not isinstance(raw, str):
                    raise ValueError
                frame = json.loads(raw)
                if not isinstance(frame, dict) or frame.get("type") not in {"item", "end", "error"}:
                    raise ValueError
                stream_id = frame.get("streamId")
                if not isinstance(stream_id, str):
                    raise ValueError
                queue = self._streams.get(stream_id)
                if queue is None:
                    continue  # in-flight frames for an already cancelled stream
                if frame["type"] == "error":
                    value = _remote_error(frame.get("error"))
                elif frame["type"] == "end":
                    value = StopAsyncIteration()
                elif "value" in frame:
                    value = frame["value"]
                else:
                    raise ValueError
                size = len(raw.encode("utf-8"))
                if self._queued_bytes + size > MAX_RESPONSE_BYTES:
                    raise asyncio.QueueFull
                queue.put_nowait((value, size))
                self._queued_bytes += size
        except asyncio.CancelledError:
            raise
        except (ValueError, TypeError, asyncio.QueueFull):
            failure = DshError("invalid_stream", "DSH 实时数据无效或积压过多，请重新读取会话。")
        except Exception:
            pass
        finally:
            if self._socket is socket:
                self._socket = None
                queues, self._streams = self._streams, {}
                for queue in queues.values():
                    # Fail the generation explicitly; never silently shed a
                    # delta and continue painting a plausible partial answer.
                    while not queue.empty():
                        _, size = queue.get_nowait()
                        self._queued_bytes -= size
                    queue.put_nowait((failure, 0))
            with suppress(Exception):
                await socket.close()

    async def export_session(self, sid: str, filename: str, limit: int) -> int:
        """Spool the official authenticated ZIP route without buffering it."""
        size = 0
        try:
            async with self._http.stream("GET", "/api/session.export", params={
                    "sessionId": native_session_id(sid), "includeDescendants": "true"}, timeout=120) as response:
                if response.status_code in {401, 403}:
                    raise DshError("auth_required", "DSH 本机认证失败，请重新配对。")
                if response.status_code != 200:
                    raise DshError("export_unavailable", "DSH 无法导出此会话，请检查导出插件。")
                with open(filename, "wb") as output:
                    async for chunk in response.aiter_bytes(chunk_size=256 * 1024):
                        size += len(chunk)
                        if size > limit:
                            raise DshError("export_too_large", "导出超过 128 MiB，请在本机 DSH 下载完整会话。")
                        output.write(chunk)
        except httpx.HTTPError:
            raise DshError("export_disconnected", "导出连接中断，请重试。") from None
        with open(filename, "rb") as source:
            if source.read(2) != b"PK":
                raise DshError("export_invalid", "DSH 返回的导出文件不是 ZIP。")
        return size

    async def stream(self, endpoint: str, args: dict | None = None) -> AsyncIterator[object]:
        if endpoint not in _STREAM_ENDPOINTS:
            raise DshError("unsupported", "此 DSH 订阅尚未适配。")
        socket = await self._ensure_socket()
        if len(self._streams) >= MAX_STREAMS:
            raise DshError("too_many_streams", "DSH 实时订阅数量已达上限。")
        stream_id = str(uuid4())
        queue: asyncio.Queue = asyncio.Queue(MAX_STREAM_ITEMS)
        self._streams[stream_id] = queue
        terminal = False
        try:
            await socket.send(json.dumps({
                "type": "open", "streamId": stream_id, "endpoint": endpoint,
                "payload": {"args": args or {}},
            }))
            while True:
                item, size = await queue.get()
                self._queued_bytes -= size
                if isinstance(item, StopAsyncIteration):
                    terminal = True
                    return
                if isinstance(item, DshError):
                    terminal = True
                    raise item
                yield item
        except DshError:
            raise
        except Exception:
            raise DshError("disconnected", "DSH 实时连接中断，请刷新会话。") from None
        finally:
            self._streams.pop(stream_id, None)
            while not queue.empty():
                _, size = queue.get_nowait()
                self._queued_bytes -= size
            if not terminal and self._socket is socket:
                with suppress(Exception):
                    await socket.send(json.dumps({"type": "cancel", "streamId": stream_id}))

    async def close(self):
        self._closed = True
        if self._socket is not None:
            await self._socket.close()
        if self._reader is not None:
            await self._reader
        await self._http.aclose()


async def exchange_launch_url(launch_url: str, *, http_transport=None) -> DshConnection:
    """Use the official token exchange. Never follow its redirect or log its URL."""
    try:
        parsed = urlsplit(launch_url)
        origin = local_origin(f"{parsed.scheme}://{parsed.netloc}")
        query = parse_qs(parsed.query, strict_parsing=True)
        token = query.get("token", [])
        if (parsed.path not in {"", "/"} or parsed.fragment
                or set(query) != {"token"} or len(token) != 1
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token[0])):
            raise ValueError
    except (ValueError, TypeError):
        raise DshError("invalid_launch_url", "请粘贴 DSH 启动时提供的完整本机登录地址。") from None
    try:
        # Use the transport directly: AsyncClient logs every request URL at
        # INFO, which would disclose the official one-time token in local logs.
        transport = http_transport or httpx.AsyncHTTPTransport(trust_env=False)
        request = httpx.Request("GET", origin + "/", params={"token": token[0]})
        try:
            async with asyncio.timeout(5):
                response = await transport.handle_async_request(request)
                response.request = request
                await response.aclose()
        finally:
            await transport.aclose()
        if response.status_code != 303 or response.headers.get("location") != "/":
            raise DshError("auth_required", "DSH 登录地址已失效，请使用当前 DSH 进程的登录地址。")
        cookies = [f"{c.name}={c.value}" for c in response.cookies.jar if c.name.startswith("dsh-auth-")]
        if len(cookies) != 1:
            raise DshError("auth_required", "DSH 未签发有效的本机配对 Cookie。")
        return DshConnection(origin, cookies[0])
    except (httpx.HTTPError, TimeoutError):
        raise DshError("disconnected", "无法连接本机 DSH；请确认服务已启动。") from None
