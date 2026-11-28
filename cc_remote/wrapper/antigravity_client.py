"""Device-local Antigravity CLI launch and ACP v1 stdio transport."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from cc_remote import __version__
from cc_remote.wrapper.antigravity_hooks import AntigravityHooks
from cc_remote.wrapper.child_env import sanitized_child_env

_MAX_FRAME = 32 * 1024 * 1024
_CONTROL_TIMEOUT = 30
_GATEWAY_URL = "AGY_LLM_GATEWAY_URL"
_GATEWAY_KEY = "AGY_LLM_GATEWAY_API_KEY"
_GATEWAY_PROTOCOL = "AGY_LLM_GATEWAY_WIRE_PROTOCOL"
_ACP_LOGS = "~/.gemini/antigravity-cli/log/acp-*.log"
_ENABLE_ENV = "CC_REMOTE_ANTIGRAVITY"
WRAPPER_ENV_KEYS = frozenset({
    "PATH", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "GEMINI_API_KEY", _GATEWAY_URL, _GATEWAY_KEY, _GATEWAY_PROTOCOL, _ENABLE_ENV,
})


def antigravity_opted_out(wrapper_environment: Mapping[str, str] | None = None) -> bool:
    return (wrapper_environment or {}).get(_ENABLE_ENV, os.environ.get(_ENABLE_ENV)) == "0"


def parse_trusted_workspaces(value: str) -> list[str] | None:
    """Decode the device's workspace allowlist or explicit all-workspaces opt-in."""
    try:
        allowed = json.loads(value)
    except (ValueError, RecursionError):
        return None
    if not isinstance(allowed, list):
        return None
    if allowed == ["*"]:
        return allowed
    if any(not isinstance(path, str) or not os.path.isabs(path) or "\0" in path for path in allowed):
        return None
    return allowed


class AcpError(Exception):
    def __init__(self, code: str, message: str, *, unknown: bool = False):
        super().__init__(message)
        self.code = code
        self.unknown = unknown


@dataclass(frozen=True)
class AntigravityLaunch:
    binary: str
    environment: dict[str, str] = field(repr=False)
    default_model: str | None = None

    @classmethod
    def local(cls, *, wrapper_environment: Mapping[str, str] | None = None) -> AntigravityLaunch | None:
        if antigravity_opted_out(wrapper_environment):
            return None
        env = sanitized_child_env()
        if wrapper_environment is not None:
            # Credentials and shell settings come from the Wrapper. Keep the
            # installed service's enable setting unless explicitly overridden.
            for key in WRAPPER_ENV_KEYS - {_ENABLE_ENV}:
                env.pop(key, None)
            env.update((key, wrapper_environment[key]) for key in wrapper_environment.keys() & WRAPPER_ENV_KEYS)
        home = Path.home()
        try:
            with (home / ".antigravity" / "settings.json").open("rb") as stream:
                settings = json.loads(stream.read(1024 * 1024))
            if not isinstance(settings, dict):
                settings = {}
        except (OSError, ValueError):
            settings = {}

        gateway = settings.get("gateway")
        if not isinstance(gateway, dict):
            gateway = {}
        # Keep the URL and credential from the same authority. The documented
        # environment also works for local setups that don't use settings.json.
        if not any(name in env for name in (_GATEWAY_URL, _GATEWAY_KEY, _GATEWAY_PROTOCOL)):
            values = [gateway.get("url"), gateway.get("apiKey"), gateway.get("wireProtocol", "genai")]
            if gateway.get("enabled") is True and all(isinstance(v, str) and v for v in values):
                env.update(zip((_GATEWAY_URL, _GATEWAY_KEY, _GATEWAY_PROTOCOL), values))
        gateway_configured = bool(env.get(_GATEWAY_URL) and env.get(_GATEWAY_KEY))
        if not (env.get(_ENABLE_ENV) == "1" or env.get("CC_REMOTE_ANTIGRAVITY_BIN")
                or gateway_configured or env.get("GEMINI_API_KEY")):
            return None
        configured = env.get("CC_REMOTE_ANTIGRAVITY_BIN")
        path = env.get("PATH", os.defpath)
        binary = shutil.which(os.path.expanduser(configured), path=path) if configured else (
            shutil.which("agy", path=path) or shutil.which(str(home / ".local" / "bin" / "agy"), path=path)
        )
        if not binary:
            return None
        models = settings.get("models")
        default = models.get("defaultModelId") if isinstance(models, dict) else None
        return cls(binary, env, default if isinstance(default, str) and 0 < len(default) <= 256 else None)


class AcpClient:
    """One reader owns response delivery. Interactive requests never block it."""

    def __init__(
        self,
        launch: AntigravityLaunch,
        on_update: Callable[[dict], Awaitable[None]],
        on_request: Callable[[str, int | str, dict], Awaitable[dict | None]],
        *,
        args: tuple[str, ...] = (),
        on_disconnect: Callable[[AcpClient], Awaitable[None]] | None = None,
        on_lifecycle: Callable[[AcpClient, str, str, str], Awaitable[None]] | None = None,
    ):
        self.launch = launch
        self.control_directory: TemporaryDirectory[str] | None = None
        self.cwd: str | None = None
        self.on_update = on_update
        self.on_request = on_request
        self.on_disconnect = on_disconnect
        self.args = args
        self.hooks = AntigravityHooks(lambda *event: on_lifecycle(self, *event)) if on_lifecycle else None
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task | None = None
        self.pending: dict[int, asyncio.Future[dict]] = {}
        self.interactions: dict[int | str, asyncio.Task] = {}
        self.write_lock = asyncio.Lock()
        self.next_id = 0
        self.capabilities: dict = {}
        self.auth_methods: list[str] = []
        self._read_ended = False
        self._closing = False

    @property
    def running(self) -> bool:
        return (not self._read_ended and not self._closing
                and self.process is not None and self.process.returncode is None
                and self.reader is not None and not self.reader.done())

    async def start(self) -> None:
        try:
            # Initialize, authenticate and discover catalogs without giving agy
            # a project from which to load executable customizations.
            self.control_directory = TemporaryDirectory(prefix="ccr-agy-control-", dir="/tmp")
            self.cwd = str(Path(self.control_directory.name).resolve())
            args = self.args
            if self.hooks:
                with suppress(OSError):
                    args += await self.hooks.start()
            self.process = await asyncio.create_subprocess_exec(
                self.launch.binary, "acp", *args, cwd=self.cwd,
                env={**self.launch.environment, "PWD": self.cwd},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=_MAX_FRAME,
            )
        except BaseException as exc:
            await self.close()
            if not isinstance(exc, OSError):
                raise
            raise AcpError("startup", "无法启动本机 agy，请检查安装和工作目录。") from None
        self.reader = asyncio.create_task(self._read(), name="antigravity-acp-reader")
        try:
            result = await self.call("initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {"elicitation": {"form": {}}},
                "clientInfo": {"name": "cc-remote", "version": __version__},
            })
            if result.get("protocolVersion") != 1:
                raise AcpError("version", "本机 agy 的 ACP 版本不兼容，请更新 Antigravity CLI。")
            capabilities = result.get("agentCapabilities")
            self.capabilities = capabilities if isinstance(capabilities, dict) else {}
            methods = result.get("authMethods")
            if isinstance(methods, list):
                offered = {item.get("id") for item in methods if isinstance(item, dict)
                           and isinstance(item.get("id"), str)}
                self.auth_methods = [name for name in ("oauth-personal", "oauth-business") if name in offered]
        except BaseException:
            await self.close()
            raise

    async def _write(self, frame: dict, *, guard: Callable[[], None] | None = None) -> None:
        # Do not include the raw frame, process environment or upstream RPC
        # messages in errors. The gateway and the agent may return credentials.
        data = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode()
        if len(data) >= _MAX_FRAME:
            raise AcpError("size", "Gemini 消息超过 ACP 大小限制。")
        async with self.write_lock:
            if not self.running:
                raise AcpError("offline", "本机 agy 已断开，请重新打开会话。")
            assert self.process is not None and self.process.stdin is not None
            # Previous writes may be buffered. Their drain does not admit this
            # frame, and a steer guard is checked again at the write boundary.
            try:
                async with asyncio.timeout(_CONTROL_TIMEOUT):
                    await self.process.stdin.drain()
            except (OSError, RuntimeError, TimeoutError):
                raise AcpError("offline", "本机 agy 已断开，请重新打开会话。") from None
            if not self.running or self.process.stdin.is_closing():
                raise AcpError("offline", "本机 agy 已断开，请重新打开会话。")
            if guard:
                guard()
            try:
                self.process.stdin.write(data + b"\n")
            except (OSError, RuntimeError):
                raise AcpError("outcome_unknown", "与本机 agy 的连接中断，发送结果未知。", unknown=True) from None

    async def submit(
        self, method: str, params: dict, *, guard: Callable[[], None] | None = None,
    ) -> asyncio.Future[dict]:
        """Return the terminal receipt after the request is written once."""
        self.next_id += 1
        identity = self.next_id
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        def forget(done):
            self.pending.pop(identity, None)
            if not done.cancelled():
                done.exception()

        future.add_done_callback(forget)
        try:
            await self._write({"jsonrpc": "2.0", "id": identity, "method": method, "params": params}, guard=guard)
        except BaseException:
            future.cancel()
            raise
        return future

    async def call(self, method: str, params: dict, *, timeout: float = _CONTROL_TIMEOUT) -> dict:
        future = await self.submit(method, params)
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise AcpError("timeout", "本机 agy 返回超时，操作结果未知。", unknown=True) from None

    async def notify(self, method: str, params: dict) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                if len(line) >= _MAX_FRAME:
                    break
                frame = json.loads(line)
                if isinstance(frame, dict) and frame.get("jsonrpc") == "2.0":
                    await self._receive(frame)
        except (OSError, ValueError, RecursionError):
            pass
        finally:
            self._read_ended = True
            for future in tuple(self.pending.values()):
                if not future.done():
                    future.set_exception(AcpError("offline", "本机 agy 连接中断，操作结果未知。", unknown=True))
            for task in tuple(self.interactions.values()):
                task.cancel()
            if not self._closing and self.on_disconnect:
                await self.on_disconnect(self)

    async def _receive(self, frame: dict) -> None:
        identity = frame.get("id")
        method = frame.get("method")
        params = frame.get("params")
        if isinstance(method, str):
            params = params if isinstance(params, dict) else {}
            if type(identity) in (int, str):
                if identity in self.interactions or len(self.interactions) >= 32:
                    if identity not in self.interactions:
                        await self._write({
                            "jsonrpc": "2.0", "id": identity,
                            "error": {"code": -32603, "message": "Too many pending interactions"},
                        })
                    return
                self.interactions[identity] = asyncio.create_task(self._answer(method, identity, params))
            elif method == "session/update":
                await self.on_update(params)
            elif method in {"$/cancel_request", "$/cancelRequest"}:
                cancelled = params.get("requestId", params.get("id"))
                task = self.interactions.get(cancelled) if type(cancelled) in (int, str) else None
                if task:
                    task.cancel()
            return
        pending = self.pending.get(identity) if type(identity) is int else None
        if pending is None or pending.done():
            return
        error, result = frame.get("error"), frame.get("result")
        if isinstance(error, dict):
            rpc_code = error.get("code")
            code, message = {
                -32000: ("authentication", "Gemini 需要认证。请在 Wrapper 主机登录 agy 或配置本机网关。"),
                -32002: ("session_missing", "本机 agy 找不到该 Gemini 会话。"),
                -32601: ("unsupported", "当前 agy 版本不支持该操作。"),
                -32602: ("rejected", "本机 agy 拒绝了会话或设置参数。"),
            }.get(rpc_code if type(rpc_code) is int else None,
                  ("rejected", "本机 agy 无法完成请求。"))
            reference = f"ACP {rpc_code}。" if type(rpc_code) is int else ""
            pending.set_exception(AcpError(code, f"{message}{reference}详情见 Wrapper 主机的 {_ACP_LOGS}。"))
        elif isinstance(result, dict) or ("result" in frame and result is None):
            pending.set_result(result or {})
        else:
            pending.set_exception(AcpError("protocol", "本机 agy 返回了无效的 ACP 响应。", unknown=True))

    async def _answer(self, method: str, identity: int | str, params: dict[str, Any]) -> None:
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": identity}
        try:
            result = await self.on_request(method, identity, params)
            if result is None:
                frame["error"] = {"code": -32601, "message": "Client method is not supported"}
            else:
                frame["result"] = result
        except asyncio.CancelledError:
            raise
        except Exception:
            frame["error"] = {"code": -32603, "message": "Client interaction could not be completed"}
        finally:
            self.interactions.pop(identity, None)
        with suppress(AcpError):
            await self._write(frame)

    async def close(self) -> None:
        self._closing = True
        interactions = tuple(self.interactions.values())
        if self.reader:
            if not self._read_ended:
                self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if interactions:
            for task in interactions:
                task.cancel()
            await asyncio.gather(*interactions, return_exceptions=True)
        process = self.process
        if process is not None:
            if process.stdin:
                process.stdin.close()
            with suppress(ProcessLookupError):
                if process.returncode is None:
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if self.hooks:
            await self.hooks.close()
        if self.control_directory is not None:
            self.control_directory.cleanup()
            self.control_directory = None
            self.cwd = None
