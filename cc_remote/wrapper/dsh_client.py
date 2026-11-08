"""DSH 0.2.2's official, local API Peer and bounded Remote transport.

Only control credentials live here. Model authentication stays entirely in DSH.
Every socket generation owns its own API Cookie; a failed write is never retried.
Cold history uses projections + page and never opens session/follow.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
from typing import AsyncIterator
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect

from cc_remote.log import logger

TESTED_DSH_VERSION = "0.2.2-rc.1"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# A follow snapshot is reconciled through bounded HTTP history reads before
# consuming its live tail. Reasoning/tool chunks can burst while those complete;
# the shared byte budget remains the hard memory boundary for every subscription.
MAX_STREAM_ITEMS = 4096
MAX_STREAMS = 64
log = logger("dsh_client")
_SID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,123}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
READ_ENDPOINTS = frozenset({
    "pluginMarket/listIndexes", "pluginMarket/search", "pluginMarket/readEntryDetail",
    "session/list", "session/search", "session/modelCatalog", "session/page",
    "session/projections", "session/events", "session/state", "session/attachment", "agentPresets/list",
    "commands/list", "skills/list", "permissionPresets/catalog", "goals/get",
    "pluginInventory/list", "pluginManager/listPlugins", "sessionReferenceResolver/candidates",
    "terminal/list", "terminal/environment", "terminal/shells", "schedule/catalog", "schedule/history", "schedule/deliveryLimits", "settings/describe", "agentPresets/read", "pluginManager/listBundles", "pluginManager/inspect", "pluginManager/listVersions", "pluginManager/readBundleDetail",
})
WRITE_ENDPOINTS = frozenset({
    "pluginMarket/setIndexes", "schedule/deleteInactive", "actionCards/act",
    "session/create", "session/prompt", "session/cancel", "session/updateQueue",
    "session/selectModel", "session/rename", "session/fork", "commands/execute",
    "fileReferences/list", "fileUploads/upload", "$events/result",
    "goals/create", "goals/edit", "goals/pause", "goals/resume", "goals/complete",
    "goals/clear", "subagents/prompt", "subagents/interruptByParent",
    "pluginManager/setPluginEnabled", "job/kill", "workspace/archiveSession", "workspace/unarchiveSession",
    "terminal/create", "terminal/rename", "terminal/close", "schedule/create", "schedule/edit", "schedule/run", "settings/update", "settings/mutate", "pluginManager/installBundle", "pluginManager/updateBundle", "pluginManager/setBundleEnabled", "pluginManager/removeBundle",
})
STREAM_ENDPOINTS = frozenset({"session/follow", "session/control", "session/observeState", "session/observeEvent", "session/observeProjections", "workspace/follow", "job/list", "job/follow", "terminal/attach", "terminal/retain", "$events"})


class DshError(RuntimeError):
    """Display-safe error: never include native payloads, URLs, or credentials."""

    def __init__(self, code: str, message: str, *, outcome_unknown: bool = False):
        super().__init__(message)
        self.code = code
        self.outcome_unknown = outcome_unknown


def native_session_id(sid: str) -> str:
    if not isinstance(sid, str) or not sid.startswith("dsh@") or not _SID.fullmatch(sid[4:]):
        raise DshError("invalid_session", "DSH 会话编号无效。")
    return sid[4:]


def wire_session_id(sid: str) -> str:
    value = "dsh@" + sid
    native_session_id(value)
    return value


def local_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or "")
        if (parsed.scheme != "http" or not address.is_loopback
                or isinstance(address, ipaddress.IPv6Address) and (address.ipv4_mapped or address.scope_id)
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or parsed.port is None or not 1 <= parsed.port <= 65535
                or any(c.isspace() for c in value)):
            raise ValueError
        host = f"[{address}]" if address.version == 6 else str(address)
        return f"http://{host}:{parsed.port}"
    except (ValueError, TypeError, AttributeError):
        raise DshError("invalid_connection", "DSH 地址须为带端口的本机 IP。") from None


@dataclass(frozen=True)
class DshConnection:
    origin: str
    token: str = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "origin", local_origin(self.origin))
        if not isinstance(self.token, str) or not _TOKEN.fullmatch(self.token):
            raise DshError("invalid_connection", "DSH 本机配对信息无效，请重新配对。")

    @classmethod
    def from_launch_url(cls, url: str) -> DshConnection:
        try:
            parsed = urlsplit(url)
            values = parse_qs(parsed.query, strict_parsing=True)
            if (parsed.path not in {"", "/"} or parsed.fragment
                    or set(values) != {"token"} or len(values["token"]) != 1):
                raise ValueError
            return cls(f"{parsed.scheme}://{parsed.netloc}", values["token"][0])
        except (ValueError, TypeError, KeyError):
            raise DshError("invalid_launch_url", "请粘贴 DSH 提供的完整本机登录地址。") from None

    @classmethod
    def load(cls, path: Path) -> DshConnection:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 8192):
                    raise ValueError
                data = json.loads(stream.read(8193))
            if not isinstance(data, dict) or set(data) != {"schema", "origin", "token"} or data["schema"] != 1:
                raise ValueError
            return cls(data["origin"], data["token"])
        except (OSError, ValueError, TypeError, KeyError):
            raise DshError("invalid_connection", "请检查 DSH 配对文件及其仅当前用户可读写的权限。") from None


def _remote_error(value: object) -> DshError:
    code = value.get("code") if isinstance(value, dict) else None
    if not isinstance(code, str) or not re.fullmatch(r"[a-zA-Z0-9_/-]{1,128}", code):
        return DshError("invalid_response", "DSH 返回了无效的错误响应。", outcome_unknown=True)
    messages = {
        "session/migration-required": "此 DSH 会话需要先在官方客户端完成格式升级。",
        "session/writer-held": "该 DSH 会话由其他原生进程持有。",
        "session/model-unavailable": "当前 DSH 模型不可用，请重新选择原生模型。",
        "gateway/not-found": "当前 DSH 未提供此接口或插件。",
        "remote-access/denied": "DSH 未授权此操作。",
        "session/agent-busy": "DSH 当前正在处理任务，请稍后重试。",
    }
    return DshError(code, messages.get(code, "DSH 未接受此操作，请检查原生会话状态。"))


@dataclass
class _Peer:
    socket: object
    http: httpx.AsyncClient
    generation: str = field(default_factory=lambda: uuid4().hex)
    streams: dict[str, asyncio.Queue] = field(default_factory=dict)
    queued_bytes: int = 0
    reader: asyncio.Task | None = None


class DshClient:
    def __init__(self, connection: DshConnection, *, http_transport=None, socket_connect=connect):
        self.connection = connection
        self._http_transport = http_transport
        self._socket_connect = socket_connect
        self._lock = asyncio.Lock()
        self._peer: _Peer | None = None
        self._closed = False

    async def _exchange(self) -> str:
        # AsyncClient's request logging includes query strings. Use only the
        # low-level transport for this official token exchange and never follow
        # its redirect. Cookies and token-bearing requests must not be logged.
        transport = self._http_transport or httpx.AsyncHTTPTransport(trust_env=False)
        request = httpx.Request("GET", self.connection.origin + "/connect", params={
            "token": self.connection.token, "interactions": "answer",
        }, headers={"Origin": self.connection.origin})
        try:
            async with asyncio.timeout(8):
                response = await transport.handle_async_request(request)
                try:
                    response.request = request
                    if (response.status_code != 302
                            or response.headers.get("location") != "./api/remote.mux"):
                        raise DshError("auth_required", "DSH 配对已失效，请重新配对当前 DSH 进程。")
                    cookies = [c for c in response.cookies.jar if c.name.startswith("dsh-auth-")]
                    audience = urlsplit(self.connection.origin).netloc
                    suffix = base64.urlsafe_b64encode(hashlib.sha256(audience.encode()).digest()).decode().rstrip("=")
                    if len(cookies) != 1 or cookies[0].name != "dsh-auth-" + suffix:
                        raise DshError("invalid_connection", "DSH 返回的认证范围不匹配。")
                    value = cookies[0].value
                    if not re.fullmatch(r"v2\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", value):
                        raise DshError("invalid_connection", "DSH 未提供独立 API 连接身份。")
                    return cookies[0].name + "=" + value
                finally:
                    await response.aclose()
        except (httpx.HTTPError, TimeoutError):
            raise DshError("disconnected", "无法连接本机 DSH。") from None
        finally:
            if self._http_transport is None:
                await transport.aclose()

    async def _connection(self) -> _Peer:
        async with self._lock:
            if self._closed:
                raise DshError("closed", "DSH 连接已关闭。")
            if self._peer is not None:
                return self._peer
            cookie = await self._exchange()
            try:
                socket = await self._socket_connect(
                    "ws" + self.connection.origin[4:] + "/api/remote.mux",
                    additional_headers={"Cookie": cookie}, origin=self.connection.origin,
                    proxy=None, open_timeout=8, close_timeout=2,
                    max_size=MAX_RESPONSE_BYTES, max_queue=16,
                )
            except Exception:
                raise DshError("disconnected", "无法建立 DSH 实时连接。") from None
            peer = _Peer(socket, httpx.AsyncClient(
                base_url=self.connection.origin, trust_env=False, follow_redirects=False,
                headers={"Cookie": cookie, "Origin": self.connection.origin},
                timeout=httpx.Timeout(30, connect=5), transport=self._http_transport,
            ))
            self._peer = peer
            peer.reader = asyncio.create_task(self._read(peer), name="dsh-api-peer")
            return peer

    async def rpc(self, endpoint: str, args: dict | None = None):
        if endpoint not in READ_ENDPOINTS | WRITE_ENDPOINTS:
            raise DshError("unsupported", "此 DSH 操作尚未适配。")
        peer = await self._connection()
        request_id = uuid4().hex
        uncertain = endpoint in WRITE_ENDPOINTS
        envelope = {"type": "client-request", "rpcId": request_id,
                    "method": endpoint, "payload": {"args": args or {}}}
        try:
            # Package operations can include a native download/build/restore. A
            # timeout is still an unknown outcome and must never trigger replay.
            timeout = 600 if endpoint in {"pluginManager/installBundle", "pluginManager/updateBundle"} else 30
            async with asyncio.timeout(timeout), peer.http.stream("POST", "/api/" + endpoint, json=envelope,
                                        timeout=httpx.Timeout(timeout, connect=5)) as response:
                if response.status_code in {401, 403}:
                    raise DshError("auth_required", "DSH 连接未获授权，请检查配对状态。")
                if response.status_code == 404:
                    raise DshError("unsupported", "当前 DSH 未提供此接口或插件。")
                if response.status_code != 200:
                    raise DshError("unavailable", "DSH 请求失败，请先核实原生状态。", outcome_unknown=uncertain)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise DshError("too_large", "DSH 响应超出读取上限。", outcome_unknown=uncertain)
                    raw.extend(chunk)
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
        except DshError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, RuntimeError):
            raise DshError("request_failed", "DSH 请求未能确认；已提交操作不会自动重发。",
                           outcome_unknown=uncertain) from None

    async def _read(self, peer: _Peer):
        failure = DshError("disconnected", "DSH 实时连接中断，正在等待重新连接。")
        batch = 0
        try:
            async for raw in peer.socket:
                if not isinstance(raw, str):
                    raise ValueError
                frame = json.loads(raw)
                if (not isinstance(frame, dict) or frame.get("type") not in {"item", "end", "error"}
                        or not isinstance(frame.get("streamId"), str)):
                    raise ValueError
                queue = peer.streams.get(frame["streamId"])
                if queue is None:
                    continue
                value = (StopAsyncIteration() if frame["type"] == "end" else
                         _remote_error(frame.get("error")) if frame["type"] == "error" else frame["value"])
                size = len(raw.encode())
                if peer.queued_bytes + size > MAX_RESPONSE_BYTES or queue.full():
                    log.warning("DSH stream buffer full", queued_items=queue.qsize(),
                                queued_bytes=peer.queued_bytes, incoming_bytes=size)
                    raise asyncio.QueueFull
                queue.put_nowait((value, size))
                peer.queued_bytes += size
                batch += 1
                if batch == 32:
                    # recv() can complete synchronously for already-buffered
                    # frames. Give all consumers a chance to drain that burst.
                    batch = 0
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except asyncio.QueueFull:
            failure = DshError("stream_overflow", "DSH 实时显示暂时积压，正在重新同步；任务仍在原生端执行。")
        except (ValueError, TypeError, KeyError):
            log.warning("DSH stream frame invalid")
            failure = DshError("invalid_stream", "DSH 实时数据格式异常，正在重新同步。")
        except Exception:
            pass
        finally:
            if self._peer is peer:
                self._peer = None
            for queue in peer.streams.values():
                while not queue.empty():
                    _, size = queue.get_nowait()
                    peer.queued_bytes -= size
                queue.put_nowait((failure, 0))
            with suppress(Exception):
                await peer.socket.close()
            await peer.http.aclose()

    async def stream(self, endpoint: str, args: dict | None = None, *, on_open=None) -> AsyncIterator[object]:
        if endpoint not in STREAM_ENDPOINTS:
            raise DshError("unsupported", "此 DSH 订阅尚未适配。")
        peer = await self._connection()
        if len(peer.streams) >= MAX_STREAMS:
            raise DshError("stream_limit", "DSH 订阅数量已达上限。")
        stream_id = uuid4().hex
        queue: asyncio.Queue = asyncio.Queue(MAX_STREAM_ITEMS)
        peer.streams[stream_id] = queue
        terminal = False
        try:
            await peer.socket.send(json.dumps({"type": "open", "streamId": stream_id,
                                              "endpoint": endpoint, "payload": {"args": args or {}}}))
            if on_open is not None:
                if endpoint != "terminal/attach":
                    raise DshError("unsupported", "此订阅不接收输入。")
                async def send(value):
                    if self._peer is not peer or stream_id not in peer.streams:
                        raise DshError("disconnected", "终端连接已失效，输入未重发。")
                    await peer.socket.send(json.dumps({"type": "item", "streamId": stream_id, "value": value}))
                on_open(send)
            while True:
                value, size = await queue.get()
                peer.queued_bytes -= size
                if isinstance(value, StopAsyncIteration):
                    terminal = True
                    return
                if isinstance(value, DshError):
                    terminal = True
                    raise value
                yield value
        finally:
            peer.streams.pop(stream_id, None)
            while not queue.empty():
                _, size = queue.get_nowait()
                peer.queued_bytes -= size
            if not terminal and self._peer is peer:
                with suppress(Exception):
                    await peer.socket.send(json.dumps({"type": "cancel", "streamId": stream_id}))

    async def list_sessions(self) -> list[dict]:
        result = await self.rpc("session/list", {"_request": {}})
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise DshError("invalid_catalog", "DSH 会话目录格式不兼容。")
        for row in result["items"]:
            if not isinstance(row, dict) or not isinstance(row.get("sessionId"), str):
                raise DshError("invalid_catalog", "DSH 会话目录格式不兼容。")
            wire_session_id(row["sessionId"])
        return result["items"]

    async def list_subagents(self, parent):
        native = native_session_id(parent)
        baseline = await self.rpc("session/projections", {"request": {"sessionId": native}})
        if not isinstance(baseline, dict) or baseline.get("kind") != "sequenced":
            raise DshError("subagents_unavailable", "DSH 子代理目录暂不可读取。")
        catalog = baseline.get("values", {}).get("subagentCatalog", [])
        rows = {row["sessionId"]: row for row in await self.list_sessions()}
        entries = []
        for child in catalog[:256]:
            row = rows.get(child["id"], {})
            local = row.get("origin") == "subagent" and row.get("parentSessionId") == native
            entries.append({**child, "activity": "running" if local and row.get("running") else "inactive",
                "hasChildren": bool(row.get("projections", {}).get("values", {}).get("subagentCatalog", [])),
                "available": local})
        return {"parentAvailable": bool(rows.get(native, {}).get("agentAvailable")), "entries": entries}

    async def history_snapshot(self, sid: str, *, before_seq=None, through_seq=None, max_messages=12,
                               query=None, commands=False, deliverables=False) -> dict:
        native = native_session_id(sid)
        baseline = await self.rpc("session/projections", {"request": {"sessionId": native}})
        if baseline is None:
            raise DshError("not_found", "DSH 会话不存在。")
        if isinstance(baseline, dict) and baseline.get("kind") == "migration-required":
            raise _remote_error({"code": "session/migration-required"})
        if (not isinstance(baseline, dict) or baseline.get("kind") != "sequenced"
                or type(baseline.get("asOfSeq")) is not int or baseline["asOfSeq"] < -1
                or not isinstance(baseline.get("values"), dict)):
            raise DshError("invalid_history", "DSH 未提供可靠的历史读取边界。")
        cursor = baseline["asOfSeq"] if through_seq is None else through_seq
        if type(cursor) is not int or not -1 <= cursor <= baseline["asOfSeq"]:
            raise DshError("invalid_history", "DSH 历史读取位置无效。")
        if type(max_messages) is not int or not 1 <= max_messages <= 500:
            raise DshError("invalid_history", "DSH 历史页大小无效。")
        metadata = None
        if query or commands or deliverables:
            metadata = await self.metadata(sid, query=query)
        if query and before_seq is None:
            if metadata.get("matchSeq") is None:
                raise DshError("search_match_changed", "搜索结果已变化，请重新搜索。")
            before_seq = metadata["matchSeq"] + 1
        address = {"kind": "session", "sessionId": native}
        # V4 delegated histories require a durable parent address. The native
        # page endpoint revalidates the lineage and descriptor before reading.
        if baseline["values"].get("subagent") is not None:
            row = next((row for row in await self.list_sessions() if row["sessionId"] == native), None)
            if not row or row.get("origin") != "subagent" or not row.get("parentSessionId"):
                raise DshError("invalid_history", "DSH 子代理的父会话身份不可确认。")
            address = {"kind": "subagent", "parentSessionId": row["parentSessionId"],
                       "childSessionId": native, "mode": "unknown"}
        request = {"address": address, "throughSeq": cursor, "maxMessages": max_messages}
        if before_seq is not None:
            if type(before_seq) is not int or not 0 <= before_seq <= cursor + 1:
                raise DshError("invalid_history", "DSH 历史分页边界无效。")
            request["beforeSeq"] = before_seq
        async def read_page(fields):
            page = await self.rpc("session/page", {"request": fields})
            if not isinstance(page, dict) or not isinstance(page.get("records"), list) or type(page.get("hasMore")) is not bool:
                raise DshError("invalid_history", "DSH 历史格式不兼容。")
            previous = -1
            for row in page["records"]:
                event = row.get("event") if isinstance(row, dict) else None
                if (not isinstance(row, dict) or row.get("type") != "event" or not isinstance(event, dict)
                        or type(event.get("seq")) is not int or not previous < event["seq"] <= cursor
                        or fields.get("beforeSeq") is not None and event["seq"] >= fields["beforeSeq"]):
                    raise DshError("invalid_history", "DSH 历史序列与读取边界不一致。")
                previous = event["seq"]
            return page
        page = await read_page(request)
        # A message-count page can start halfway through a tool-heavy turn.
        # Recover only its prefix, bounded, so user/step identities are exact.
        while page["hasMore"] and page["records"] and page["records"][0]["event"]["type"] != "turn/start":
            first = page["records"][0]["event"]["seq"]
            prefix = await read_page({**request, "beforeSeq": first, "maxMessages": 100})
            if not prefix["records"]:
                raise DshError("invalid_history", "DSH 历史分页未前进。")
            starts = [i for i, row in enumerate(prefix["records"]) if row["event"]["type"] == "turn/start"]
            offset = starts[-1] if starts else 0
            page = {**page, "records": prefix["records"][offset:] + page["records"],
                    "hasMore": prefix["hasMore"] or offset > 0}
            if len(page["records"]) > 40000 or len(json.dumps(page).encode()) > MAX_RESPONSE_BYTES:
                raise DshError("history_too_large", "DSH 此轮历史超过安全读取上限，请使用原生导出。")
            if starts:
                break
        # Native hasMore means earlier journal records, which can be only the
        # permission/model setup preceding the very first conversation. Verify
        # that there is an older turn before offering conversation pagination.
        # One native message is enough in the ordinary case (including the
        # previous turn's terminal); do not load the previous turn's detail.
        if page["hasMore"]:
            if not page["records"]:
                raise DshError("invalid_history", "DSH 历史分页未前进。")
            before = page["records"][0]["event"]["seq"]
            scanned_records = scanned_bytes = 0
            while True:
                older = await read_page({**request, "beforeSeq": before, "maxMessages": 1})
                scanned_records += len(older["records"])
                scanned_bytes += len(json.dumps(older).encode())
                if scanned_records > 40000 or scanned_bytes > MAX_RESPONSE_BYTES:
                    raise DshError("history_too_large", "DSH 历史读取超过安全上限，请使用原生导出。")
                if any(row["event"]["type"] in {"turn/start", "turn/end", "assistant/message"}
                       for row in older["records"]):
                    break
                if not older["hasMore"]:
                    page = {**page, "hasMore": False}
                    break
                if not older["records"]:
                    raise DshError("invalid_history", "DSH 历史分页未前进。")
                before = older["records"][0]["event"]["seq"]
        return {**page, "cursor": cursor, "projections": baseline,
                **({"commands": metadata["commands"]} if commands else {}),
                **({"deliverables": metadata["deliverables"]} if deliverables else {})}

    async def draft_catalog(self, cwd, preset=None):
        value = await self.read_route("/api/cc-remote.catalog", {"cwd": cwd, **({"preset": preset} if preset else {})})
        if not isinstance(value, dict) or value.get("contract") != 3 or value.get("version") != TESTED_DSH_VERSION:
            raise DshError("metadata_required", f"请加载 DSH {TESTED_DSH_VERSION} 对应的 cc-remote 元数据插件。")
        return value

    async def metadata(self, sid: str, *, query=None):
        params = {"sessionId": native_session_id(sid)}
        if query:
            params["query"] = query
        value = await self.read_route("/api/cc-remote.metadata", params)
        if (not isinstance(value, dict) or value.get("contract") != 3
                or value.get("version") != TESTED_DSH_VERSION
                or value.get("sessionId") != native_session_id(sid)):
            raise DshError("metadata_required", f"请加载与 DSH {TESTED_DSH_VERSION} 对应的 cc-remote 元数据插件。")
        return value

    async def read_route(self, route: str, params: dict):
        if route not in {"/api/changes.summary", "/api/changes.diff", "/api/cc-remote.metadata", "/api/cc-remote.catalog"}:
            raise DshError("unsupported", "DSH 读取路径尚未适配。")
        peer = await self._connection()
        try:
            async with peer.http.stream("GET", route, params=params) as response:
                if response.status_code == 404:
                    return None
                if response.status_code != 200:
                    raise DshError("read_failed", "DSH 原生数据暂不可读取，请检查连接和插件。")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise DshError("too_large", "DSH 数据超过读取上限。")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (httpx.HTTPError, ValueError):
            raise DshError("read_failed", "DSH 原生数据读取失败。") from None

    async def create_schedule(self, request):
        return await self.rpc("schedule/create", {"request": request})

    async def workspace_changes(self, sid, seq, index=None):
        if type(seq) is not int or not 0 <= seq <= 9007199254740991 or (index is not None and (type(index) is not int or not 0 <= index < 4096)):
            raise DshError("invalid_changes", "DSH 改动坐标无效。")
        return await self.read_route("/api/changes.summary" if index is None else "/api/changes.diff",
            {"sessionId": native_session_id(sid), "seq": seq, **({"index": index} if index is not None else {})})

    async def export_session(self, sid, filename, limit):
        peer = await self._connection()
        size = 0
        try:
            async with peer.http.stream("GET", "/api/session.export", params={
                    "sessionId": native_session_id(sid), "includeDescendants": "true"}, timeout=120) as response:
                if response.status_code != 200:
                    raise DshError("export_unavailable", "DSH 无法导出此会话。")
                with open(filename, "wb") as output:
                    async for chunk in response.aiter_bytes(chunk_size=256 * 1024):
                        size += len(chunk)
                        if size > limit:
                            raise DshError("export_too_large", "导出超过 128 MiB，请在本机 DSH 导出。")
                        output.write(chunk)
        except httpx.HTTPError:
            raise DshError("export_failed", "DSH 导出失败，请重新尝试。") from None
        with open(filename, "rb") as source:
            if source.read(2) != b"PK":
                raise DshError("export_invalid", "DSH 未返回 ZIP 文件。")
        return size

    async def close(self):
        async with self._lock:
            self._closed = True
            peer = self._peer
            if peer is not None:
                await peer.socket.close()
                if peer.reader is not None:
                    await peer.reader
