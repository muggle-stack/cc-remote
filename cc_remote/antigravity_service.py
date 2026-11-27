"""Optional private Gemini worker. Its ACP sessions outlive a Wrapper connection."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import signal
import stat
from contextlib import ExitStack, asynccontextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4

from cc_remote.claude_service.wire import private_directory, read_frame, same_user, write_frame
from cc_remote.log import logger
from cc_remote import protocol
from cc_remote.protocol import AntigravityAuthResult, AntigravityAvailability, FilePreview, SessionInfo
from cc_remote.update import UpdateError
from cc_remote.update_jobs import read_json, write_json
from cc_remote.wrapper.antigravity_client import AcpError, AntigravityLaunch, WRAPPER_ENV_KEYS, antigravity_opted_out
from cc_remote.wrapper.media_preview import MediaRegistration
from cc_remote.wrapper.session_presentation import SessionPresentationStore, SessionPresentationStoreError

log = logger("antigravity_service")
_REPLAY = ContextVar("gemini_replay", default=False)
_DISCONNECT = (OSError, ValueError, asyncio.IncompleteReadError, TimeoutError)
_SOCKET = "service.sock"
GEMINI_SERVICE_VERSION = 3
GEMINI_CONTROL_VERSION = 1
GEMINI_SWITCH_FENCE = "service-switching"


def service_message(event):
    """The local contract is versioned independently of browser chat."""
    fields = json.loads(protocol.serialize(event))
    fields.pop("v", None)
    return fields


def unavailable_media(event: FilePreview) -> FilePreview:
    return event.model_copy(update={"media_id": None, "error": (
        "视频暂时无法读取，请重新打开文件。" if event.format == "video" else event.error)})


def local_message(fields):
    try:
        if not isinstance(fields, dict) or "v" in fields:
            raise ValueError("invalid Gemini service message")
        # Additive fields belong to the sending release. The local version
        # must change if a new field is required for correct interpretation.
        return protocol.deserialize(
            json.dumps({**fields, "v": protocol.PROTOCOL_VERSION}, ensure_ascii=False),
            allow_additive_fields=True,
        )
    except (protocol.ProtocolError, TypeError, ValueError):
        raise AcpError("service_protocol", "Gemini 本机服务消息格式不兼容，请检查服务版本。", unknown=True) from None


def validate_socket(path: Path):
    info, root = path.lstat(), path.parent.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or not stat.S_ISDIR(root.st_mode)
            or any(item.st_uid != os.getuid() or item.st_mode & 0o077 for item in (info, root))):
        raise PermissionError("untrusted Gemini service socket")


@asynccontextmanager
async def service_control(path: Path, machine: str, op: str):
    validate_socket(path)
    writer = None
    try:
        async with asyncio.timeout(3):
            reader, writer = await asyncio.open_unix_connection(str(path))
            if not same_user(writer):
                raise PermissionError("untrusted Gemini service process")
            await write_frame(writer, {"op": op, "protocol": GEMINI_CONTROL_VERSION, "machine": machine})
            reply = await read_frame(reader)
        yield reply
    finally:
        if writer:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()


class ServiceTransport:
    """A detached controller has no event sink. The machine retains its own rings."""

    def __init__(self):
        self.writer: asyncio.StreamWriter | None = None
        self.lock = asyncio.Lock()
        self.on_connected = None
        self.media_previews = None

    async def write(self, writer, frame):
        async with self.lock:
            if self.writer is not writer:
                return
            try:
                async with asyncio.timeout(3):
                    await write_frame(writer, frame)
            except _DISCONNECT:
                self.writer = None
                writer.transport.abort()

    async def send(self, event):
        if _REPLAY.get() and event.type != "antigravity_availability" and not (event.sid or "").startswith("agy@"):
            return
        if (writer := self.writer) is not None:
            frame = {"event": service_message(event)}
            if isinstance(event, FilePreview) and event.media_id and self.media_previews is not None:
                with suppress(ValueError):
                    if source := self.media_previews.gemini_registration(event.media_id):
                        frame["media_registration"] = source.model_dump(mode="json")
            await self.write(writer, frame)


class GeminiService:
    def __init__(self, machine, transport, *, message_state=None):
        self.machine = machine
        self.transport = transport
        self.transport.media_previews = machine.media_previews
        self.message_state = message_state or machine._antigravity.metadata.root
        self.environment: dict[str, str] | None = None
        self.machine._antigravity.opted_out = lambda: antigravity_opted_out(self.environment)
        self.attach_lock = asyncio.Lock()
        self.commands = set()
        self.processing = {}
        self.drain_owner = None

    def status(self):
        messages = self.machine._session_messages
        return {
            "ready": self.machine._antigravity.available(),
            "busy": bool(self.commands or self.machine._ui_updates.busy_count()
                         or (messages and messages.active)),
            "state_dir": str(self.machine._antigravity.metadata.root.resolve()),
            "release": str(Path(__file__).resolve().parents[1]),
        }

    def _import_completion_receipts(self):
        owner = self.machine.cfg.antigravity_owner_state_dir
        store = self.machine._session_presentation
        if owner is None or store is None:
            return
        marker = Path(self.machine.cfg.state_dir) / "gemini-completion-import.json"
        try:
            try:
                read_json(marker, os.getuid())
            except FileNotFoundError:
                source = SessionPresentationStore(owner).entries_for_engine("antigravity")
                store.import_completions("antigravity", source)
                write_json(marker, {"version": 1}, mode=0o600)
        except (OSError, ValueError, UpdateError, SessionPresentationStoreError) as exc:
            log.warning("Gemini completion receipt migration unavailable", error_type=type(exc).__name__)

    async def attach(self, writer, settings):
        if not isinstance(settings, dict) or not isinstance(settings.get("session_messages"), bool):
            await write_frame(writer, {"error": "scope"})
            return False
        environment = settings.get("environment")
        trusted_workspaces = settings.get("trusted_workspaces")
        if (not isinstance(environment, dict) or environment.keys() - WRAPPER_ENV_KEYS
                or any(not isinstance(value, str) or "\0" in value for value in environment.values())
                or sum(len(value) for value in environment.values()) > 131072
                or not isinstance(trusted_workspaces, str) or len(trusted_workspaces) > 131072):
            await write_frame(writer, {"error": "scope"})
            return False
        async with self.attach_lock:
            if self.transport.writer is not None or self.machine._antigravity.admission_paused():
                await write_frame(writer, {"error": "occupied"})
                return False
            messages = self.machine._session_messages
            if settings["session_messages"] and messages is None:
                from cc_remote.wrapper.session_messages import SessionMessages

                messages = SessionMessages(self.machine, gemini_worker_state=self.message_state)
                try:
                    await messages.start()
                except BlockingIOError:
                    # The old in-process Wrapper may still hold the receiver.
                    await write_frame(writer, {"error": "occupied"})
                    return False
                self.machine._session_messages = messages
            if messages is not None:
                messages.set_enabled(settings["session_messages"])
            # Read the old owner only after its replacement can attach, so
            # completions produced between installation and takeover survive.
            await asyncio.to_thread(self._import_completion_receipts)
            self.environment = environment
            self.machine.cfg.antigravity_trusted_workspaces = trusted_workspaces
            # No events can precede the attach reply.
            self.transport.writer = writer
            await self.transport.write(writer, {"attached": True})
        return True

    async def connection(self, reader, writer):
        try:
            if not same_user(writer):
                return
            async with asyncio.timeout(3):
                attach = await read_frame(reader)
                op = attach.get("op")
                version = GEMINI_CONTROL_VERSION if op in {"status", "drain"} else GEMINI_SERVICE_VERSION
                expected = {"protocol": version, "machine": self.machine.cfg.machine_id}
                if any(attach.get(key) != value for key, value in expected.items()):
                    await write_frame(writer, {"error": "scope"})
                    return
                if op == "status":
                    await write_frame(writer, self.status())
                    return
                if op == "drain":
                    if self.drain_owner is not None or self.attach_lock.locked() or self.status()["busy"]:
                        await write_frame(writer, {"error": "busy"})
                        return
                    self.drain_owner = writer
                    self.machine._antigravity.draining = True
                    await write_frame(writer, {"draining": True,
                                              "state_dir": str(self.machine._antigravity.metadata.root.resolve())})
                elif op != "attach":
                    await write_frame(writer, {"error": "scope"})
                    return
                elif not await self.attach(writer, attach.get("runtime")):
                    return
            if self.drain_owner is writer:
                await reader.read(1)
                return
            while True:
                request = await read_frame(reader)
                if self.machine._antigravity.admission_paused():
                    await self.transport.write(writer, {"id": request.get("id"), "error": "draining"})
                    continue
                if len(self.commands) >= 128:
                    await self.transport.write(writer, {"id": request.get("id"), "error": "busy"})
                    continue
                task = asyncio.create_task(self.command(writer, request))
                self.commands.add(task)
                task.add_done_callback(self.commands.discard)
        except _DISCONNECT:
            pass
        finally:
            if self.drain_owner is writer:
                self.drain_owner = None
                self.machine._antigravity.draining = False
            if self.transport.writer is writer:
                self.transport.writer = None
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def command(self, writer, request):
        identity = request.get("id")
        try:
            if request.get("op") == "list_sessions":
                result = [session.model_dump(mode="json") for session in await self.machine._antigravity.list_sessions()]
            elif request.get("op") == "check_media":
                await asyncio.to_thread(self._check_media, request.get("media_id"), request.get("owner_id"))
                result = True
            elif request.get("op") == "session_cwd":
                sid = request.get("sid")
                if not isinstance(sid, str) or not sid.startswith("agy@"):
                    raise ValueError("not a Gemini session")
                result = await self.machine._antigravity.session_cwd(sid)
            else:
                result = await self.handle_command(local_message(request["command"]),
                                                   reliable=request.get("reliable") is True)
            frame = {"id": identity, "reply": result}
        except Exception as exc:
            log.warning("Gemini service request failed", error_type=type(exc).__name__)
            frame = {"id": identity, "error": "command"}
        await self.transport.write(writer, frame)

    def _check_media(self, media_id, owner_id):
        store = self.machine.media_previews
        if store.get(media_id, owner_id).engine != "antigravity":
            raise FileNotFoundError("video unavailable")
        with store.open(media_id, owner_id)[0]:
            pass

    async def handle_command(self, cmd, *, reliable=False):
        if cmd.type == "hello" and cmd.role == "client":
            token = _REPLAY.set(True)
            try:
                await self.machine._handle_client_hello(cmd)
            finally:
                _REPLAY.reset(token)
            return []
        if not self.machine._antigravity.targets(cmd):
            raise ValueError("not a Gemini command")
        if reliable:
            key = (cmd.client_id, cmd.cmd_id)
            if active := self.processing.get(key):
                await asyncio.shield(active)
            else:
                self.processing[key] = asyncio.current_task()
                try:
                    await self.machine._process_command(cmd)
                finally:
                    self.processing.pop(key, None)
            return []
        result = await self.machine._handle(cmd)
        responses = result if isinstance(result, (tuple, list)) else (result,)
        return [service_message(item) for item in responses if item is not None]

    async def close(self):
        writer = self.transport.writer
        self.transport.writer = None
        if writer:
            writer.transport.abort()
        for command in tuple(self.commands):
            command.cancel()
        await asyncio.gather(*self.commands, return_exceptions=True)
        if self.machine._session_messages:
            await self.machine._session_messages.close()
        await self.machine._command_scheduler.close()
        await self.machine._antigravity.close()


class GeminiController:
    """Wrapper-side client for one independently supervised service."""

    def __init__(self, path, machine):
        self.path = Path(path).expanduser()
        self.machine = machine
        self.writer = None
        self.reader_task = None
        self.lock = asyncio.Lock()
        self.pending = {}
        self._login_guards: dict[tuple[str | None, str | None], ExitStack] = {}

    async def _connect(self):
        if self.reader_task is not None and not self.reader_task.done() and self.writer is not None:
            return
        writer = None
        try:
            validate_socket(self.path)
            settings = {
                "environment": {key: os.environ[key] for key in WRAPPER_ENV_KEYS if key in os.environ},
                "session_messages": self.machine.cfg.session_messages_enabled,
                "trusted_workspaces": self.machine.cfg.antigravity_trusted_workspaces,
            }
            async with asyncio.timeout(3):
                while True:
                    reader, writer = await asyncio.open_unix_connection(str(self.path))
                    if not same_user(writer):
                        raise ValueError("untrusted Gemini service process")
                    await write_frame(writer, {"op": "attach", "protocol": GEMINI_SERVICE_VERSION,
                                              "machine": self.machine.cfg.machine_id, "runtime": settings})
                    reply = await read_frame(reader)
                    if reply.get("attached") is True:
                        break
                    if reply.get("error") != "occupied":
                        raise ValueError("Gemini service configuration mismatch")
                    # During Wrapper replacement the service may not have
                    # observed EOF on the old controller yet. Retry only the
                    # attach handshake, before sending any command.
                    writer.close()
                    await writer.wait_closed()
                    await asyncio.sleep(0.1)
            self.writer = writer
            self.reader_task = asyncio.create_task(self._read(reader, writer))
        except _DISCONNECT:
            if writer:
                writer.transport.abort()
            raise AcpError("service", "无法连接本机 Gemini 服务。请检查服务状态和版本。") from None

    async def _read(self, reader, writer):
        disconnect = "本机 Gemini 服务连接中断。"
        try:
            while True:
                frame = await read_frame(reader)
                if "event" in frame:
                    event = local_message(frame["event"])
                    if isinstance(event, FilePreview) and event.media_id:
                        event = self._register_media(event, frame.get("media_registration"))
                    elif isinstance(event, AntigravityAuthResult):
                        # The browser refreshes quota as soon as it sees the
                        # result, which can precede the worker's command reply.
                        if guard := self._login_guards.get((event.to, event.request_id)):
                            guard.close()
                        elif event.signed_in:
                            self.machine._antigravity.quota.invalidate()
                    await self.machine.transport.send(event)
                elif (future := self.pending.get(frame.get("id"))) and not future.done():
                    if "error" in frame:
                        message = ("Gemini 服务正在切换，请稍后重试。" if frame["error"] == "draining"
                                   else "本机 Gemini 服务未完成请求。")
                        future.set_exception(AcpError("service", message))
                    else:
                        future.set_result(frame["reply"])
        except _DISCONNECT:
            pass
        except AcpError as exc:
            disconnect = str(exc)
        finally:
            if self.writer is writer:
                self.writer = None
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(AcpError("service", disconnect, unknown=True))
            writer.transport.abort()

    def _register_media(self, event: FilePreview, registration) -> FilePreview:
        try:
            source = MediaRegistration.model_validate(registration)
            if source.media_id != event.media_id or source.sid != event.sid or not source.sid.startswith("agy@"):
                raise ValueError("video registration scope mismatch")
            self.machine.media_previews.register_gemini(source, lambda: self._check_media(source))
            return event
        except ValueError:
            return unavailable_media(event)

    async def _check_media(self, source: MediaRegistration) -> None:
        try:
            async with asyncio.timeout(5):
                if await self.request({"op": "check_media", "media_id": source.media_id,
                                       "owner_id": source.owner}) is True:
                    return
        except (AcpError, TimeoutError):
            pass
        raise FileNotFoundError("video unavailable")

    async def request(self, fields, *, timeout=90):
        key = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        writer = None
        try:
            async with self.lock:
                await self._connect()
                writer = self.writer
                self.pending[key] = future
                async with asyncio.timeout(5):
                    await write_frame(writer, {"id": key, **fields})
            return await asyncio.wait_for(future, timeout)
        except _DISCONNECT:
            if writer is not None and self.writer is writer:
                writer.transport.abort()
            raise AcpError("service", "本机 Gemini 服务响应中断。", unknown=True) from None
        finally:
            self.pending.pop(key, None)
            if not future.done():
                future.cancel()

    async def _request_command(self, cmd, *, reliable=False):
        payload = {"command": service_message(cmd)}
        if reliable:
            payload["reliable"] = True
        is_auth = cmd.type == "antigravity_auth"
        timeout = 310 if is_auth else 90
        key = (cmd.client_id, cmd.cmd_id) if is_auth and cmd.method else None
        with ExitStack() as guard:
            if key is not None and key not in self._login_guards:
                guard.enter_context(self.machine._antigravity.quota.account_change())
                if cmd.client_id and cmd.cmd_id:
                    self._login_guards[key] = guard
            try:
                return await self.request(payload, timeout=timeout)
            finally:
                if key is not None and self._login_guards.get(key) is guard:
                    del self._login_guards[key]

    async def call(self, cmd):
        reply = await self._request_command(cmd)
        events = [local_message(raw) for raw in reply]
        return [unavailable_media(event) if isinstance(event, FilePreview) and event.media_id
                and self.machine.media_previews.gemini_registration(event.media_id) is None else event
                for event in events]

    async def list_sessions(self):
        return [SessionInfo.model_validate(row, extra="ignore")
                for row in await self.request({"op": "list_sessions"})]

    async def session_cwd(self, sid):
        return await self.request({"op": "session_cwd", "sid": sid})

    async def dispatch(self, cmd):
        try:
            values = await self.call(cmd)
            return None if not values else values[0] if len(values) == 1 else tuple(values)
        except AcpError as exc:
            return await self.machine._antigravity.error(cmd, exc)

    async def process(self, cmd):
        try:
            await self._request_command(cmd, reliable=True)
        except AcpError as exc:
            await self.machine._antigravity.error(cmd, exc)
            if not exc.unknown:
                await self.machine._send_command_ack(cmd.client_id, cmd.cmd_id)

    async def replay(self, hello):
        try:
            await self.call(hello)
        except AcpError:
            if hello.discover_engines:
                await self.machine.transport.send(AntigravityAvailability(
                    enabled=False, to=hello.client_id, route_id=hello.route_id))

    async def close(self):
        if self.writer:
            self.writer.transport.abort()
        if self.reader_task:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)


async def serve(directory: Path, *, wrapper_state: Path | None = None, machine_id: str | None = None,
                cwd: str | None = None):
    from cc_remote.config import WrapperConfig, valid_machine_id
    from cc_remote.wrapper.machine import WrapperMachine

    private_directory(directory)
    path = directory / _SOCKET
    if len(os.fsencode(path)) > 103:
        raise ValueError("Gemini service socket path is too long")
    lock_fd = os.open(directory / "service.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise PermissionError("Refusing to replace an unrelated service socket")
            path.unlink()
        cfg = WrapperConfig()
        cfg.state_dir = (wrapper_state or cfg.state_dir).expanduser().resolve()
        if machine_id is not None:
            if not valid_machine_id(machine_id):
                raise ValueError("invalid Gemini device id")
            cfg.machine_id = machine_id
        if cwd is not None:
            cfg.cc_cwd = cwd
        if cfg.state_dir.resolve() == directory.resolve():
            raise ValueError("Gemini requires a dedicated service state directory")
        cfg.antigravity_owner_state_dir = cfg.state_dir
        cfg.state_dir = directory
        cfg.antigravity_service_socket = ""
        cfg.session_messages_enabled = False
        transport = ServiceTransport()
        machine = WrapperMachine(cfg, transport)
        machine._antigravity.install_fence = directory / GEMINI_SWITCH_FENCE
        service = GeminiService(machine, transport, message_state=cfg.antigravity_owner_state_dir)
        machine._antigravity.launch_factory = lambda: AntigravityLaunch.local(wrapper_environment=service.environment)
        try:
            server = await asyncio.start_unix_server(service.connection, str(path))
            try:
                path.chmod(0o600)
                stopped = asyncio.Event()
                loop = asyncio.get_running_loop()
                for signum in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(signum, stopped.set)
                await stopped.wait()
            finally:
                server.close()
                await server.wait_closed()
        finally:
            await service.close()
            path.unlink(missing_ok=True)
    finally:
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--wrapper-state-dir", type=Path)
    parser.add_argument("--machine-id")
    parser.add_argument("--cwd")
    args = parser.parse_args()
    asyncio.run(serve(args.state_dir.expanduser().absolute(), wrapper_state=args.wrapper_state_dir,
                      machine_id=args.machine_id, cwd=args.cwd))


if __name__ == "__main__":
    main()
