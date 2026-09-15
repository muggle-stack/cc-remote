"""Confirmed native deletion, including Codex Code's archive prerequisite."""

import asyncio
from dataclasses import dataclass
import uuid

from cc_remote.protocol import ArchiveSession
from cc_remote.tui import _safe_remote_text


@dataclass
class Receipt:
    command: object
    future: asyncio.Future
    acked: bool = False
    confirmed: bool = False


class SessionDeletion:
    TIMEOUT = 30

    def __init__(self, client):
        self.client = client
        self.pending = {}
        self.tasks = {}

    def start(self, command):
        sid = self.client.workspace.rekeys.get(
            command.session_id, command.session_id
        )
        command = command.model_copy(update={"session_id": sid})
        if sid in self.tasks:
            self.client.notice = "Deletion already pending for this session"
            return False
        row = self.client.workspace.catalog.get(sid)
        if row is None or (row.get("engine"), row.get("space", "code")) != (
            command.engine, command.space
        ):
            self.client.notice = "Session scope changed; reopen the session tree"
            return False
        archive = (command.engine == "codex" and command.space == "code"
                   and row.get("tag") != "archived")
        self.tasks[sid] = asyncio.create_task(self.run(command, archive))
        self.client.notice = (
            "Archiving before deletion…" if archive
            else "Delete requested; waiting for server confirmation"
        )
        return True

    async def wait_for(self, command):
        command = command.model_copy(update={
            "cmd_id": uuid.uuid4().hex, "client_id": self.client.client_id,
        })
        receipt = Receipt(command, asyncio.get_running_loop().create_future())
        self.pending[command.cmd_id] = receipt
        try:
            if not await self.client._send(command):
                raise ValueError("Command could not be queued")
            await asyncio.wait_for(receipt.future, timeout=self.TIMEOUT)
        finally:
            self.pending.pop(command.cmd_id, None)
            if not receipt.future.done():
                receipt.future.cancel()

    async def run(self, command, archive):
        sid = command.session_id
        stage = "Archive" if archive else "Deletion"
        try:
            if archive:
                await self.wait_for(ArchiveSession(
                    session_id=sid, engine=command.engine,
                    space=command.space, archived=True,
                ))
            stage = "Deletion"
            self.client.notice = "Deleting; waiting for server confirmation…"
            await self.wait_for(command)
            # Only the correlated successful deletion may retire a local tab.
            self.client.workspace.catalog.pop(sid, None)
            self.client.buffers.close(sid)
            if self.client.attached_sid == sid:
                self.client.attached_sid = None
                self.client.restore_pending = False
            self.client.save_tabs()
            self.client.notice = "Session deleted"
        except TimeoutError:
            self.client.notice = (
                f"{stage} not confirmed; refresh before retrying. "
                + ("Deletion was not sent." if stage == "Archive" else "")
            )
        except Exception as exc:
            self.client.notice = f"{stage} failed: {_safe_remote_text(str(exc))}"
        finally:
            self.tasks.pop(sid, None)

    def observe(self, event):
        kind = event.get("type")
        if kind == "command_ack":
            if event.get("client_id") != self.client.client_id:
                return
            request = event.get("cmd_id")
        else:
            request = event.get("request_id")
        receipt = self.pending.get(request)
        if receipt is None or receipt.future.done():
            return
        command = receipt.command
        if kind == "error":
            receipt.future.set_exception(ValueError(
                event.get("message") or "Server rejected the command"
            ))
            return
        if kind == "command_ack":
            receipt.acked = True
        elif kind == "session_list" and (
            event.get("engine"), event.get("space", "code")
        ) == (command.engine, command.space):
            if any(profile.get("error") for profile in event.get(
                command.engine + "_profiles", []
            )):
                receipt.confirmed = False
                return  # An unavailable catalog cannot prove absence.
            row = next((row for row in event.get("sessions", [])
                        if row.get("session_id") == command.session_id), None)
            receipt.confirmed = (
                row is not None and row.get("tag") == "archived"
                if command.type == "archive_session" else row is None
            )
        # An ACK means handled, not successful. Errors and the exact catalog
        # reply precede it; neither an unrelated list nor ACK alone is proof.
        if receipt.acked and receipt.confirmed:
            receipt.future.set_result(None)

    def close(self):
        for task in list(self.tasks.values()):
            task.cancel()
