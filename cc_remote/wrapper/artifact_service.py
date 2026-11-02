"""Requester-scoped Artifacts reads and bounded live discovery."""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from cc_remote.attachments import MAX_SINGLE_ATTACHMENT_BYTES
from cc_remote.log import logger
from cc_remote.protocol import (
    AssistantMsgEnd, Delta, FilePreview, GetFilePreview, GetHistory,
    GetHistoryImage, ProcessEvent, SessionArtifactInfo, SessionArtifacts, TurnEnd, TurnSteered, UserMsg,
)
from cc_remote.viewer_pages import PageScope
from cc_remote.wrapper.session_artifacts import (
    ArtifactStorageFull, SessionArtifactStore, image_thumbnail, public_entry,
)

log = logger("cc_remote.wrapper.artifacts")


class ArtifactService:
    def __init__(self, machine):
        self.machine = machine
        self._store = None

    @property
    def store(self):
        if self._store is None:
            self._store = SessionArtifactStore(self.machine.cfg.state_dir)
        return self._store

    def scope(self, engine, space, sid):
        # Wire SIDs include account identity; never use the unqualified native UUID.
        return json.dumps([self.machine.cfg.machine_id, engine, space, sid])

    def context_scope(self, ctx, sid=None):
        return self.scope(ctx.engine, ctx.space, sid or self.machine._ctx_wire_sid(ctx) or ctx.key)

    async def uploads(self, ctx, msg_id, files=None, images=None):
        try:
            await asyncio.to_thread(self.store.uploads, self.context_scope(ctx), msg_id, files, images)
        except ArtifactStorageFull as exc:
            log.warning("artifact upload copy skipped: storage limit", reason=str(exc))
        except Exception as exc:
            log.warning("artifact upload copy unavailable", error_type=type(exc).__name__)

    async def observe(self, ctx, msg):
        scope = self.context_scope(ctx)
        if isinstance(msg, (UserMsg, TurnSteered)) and msg.images:
            if not await asyncio.to_thread(self.store.has_uploads, scope, msg.msg_id):
                await self.uploads(ctx, msg.msg_id, images=msg.images)
        elif isinstance(msg, Delta) and msg.channel != "thinking" and ctx.engine != "dsh":
            # Discovery never holds full long answers or reasoning streams in RAM.
            pending = ctx.artifact_text
            pending[msg.message_id] = ("" if msg.replace else pending.get(msg.message_id, "")) + msg.text
            pending[msg.message_id] = pending[msg.message_id][-128 * 1024:]
            while len(pending) > 8:
                pending.pop(next(iter(pending)))
        elif isinstance(msg, AssistantMsgEnd):
            text = ctx.artifact_text.pop(msg.message_id, "")
            if text and msg.channel != "thinking":
                await asyncio.to_thread(self.store.text, scope, text, ctx.cwd)
        elif (isinstance(msg, ProcessEvent) and msg.phase == "end"
              and msg.status == "succeeded" and msg.tool in {"image_generation", "imagegeneration"}
              and isinstance(msg.input, dict)):
            path = msg.input.get("file_path") or msg.input.get("path")
            if isinstance(path, str):
                await asyncio.to_thread(self.store.text, scope, f"[image](<{path}>)", ctx.cwd)
        elif isinstance(msg, TurnEnd):
            for text in ctx.artifact_text.values():
                await asyncio.to_thread(self.store.text, scope, text, ctx.cwd)
            ctx.artifact_text.clear()

    async def resolve(self, cmd):
        machine = self.machine
        if not cmd.sid:
            raise ValueError("请先打开一个会话")
        scope, cwd = await machine._viewer_page_scope(PageScope(
            sid=cmd.sid, engine=cmd.engine, space=cmd.space))
        if not cwd:
            raise ValueError("会话目录不可用")
        return scope.sid, cwd, self.scope(scope.engine, scope.space, scope.sid)

    async def list(self, cmd):
        machine = self.machine
        response = SessionArtifacts(request_id=cmd.request_id, sid=cmd.sid, to=cmd.client_id)
        try:
            sid, cwd, scope = await self.resolve(cmd)
            response.sid = sid
            if cmd.scan_history:
                ctx = machine._ctx_for(sid)
                private_dsh = cmd.engine == "dsh" and sid in machine._dsh.sidechats.check()
                if not private_dsh and (ctx is None or not ctx.btw):
                    request = GetHistory(session_id=sid, before=cmd.history_before, limit=12, detail="summary", cwd=cwd)
                    history = (await machine._dsh.handle_get_history(request, publish=False) if cmd.engine == "dsh"
                               else await machine._history_page_singleflight(request, sid))
                    if history.error:
                        raise ValueError("历史附件整理暂时失败，请重试")
                    await asyncio.to_thread(self.store.history, scope, history, cwd,
                                            discover_text=cmd.engine != "dsh")
                    response.history_before = history.oldest_id
                    response.history_more = history.has_more
            if cmd.engine == "dsh":
                metadata = await machine._dsh.client.metadata(sid)
                await asyncio.to_thread(self.store.native_outputs, scope, metadata["deliverables"], cwd)
            rows, response.next_before, response.has_more = await asyncio.to_thread(self.store.page, scope, cmd.before)
            response.entries = await asyncio.to_thread(lambda: [SessionArtifactInfo(**public_entry(row)) for row in rows])
        except Exception as exc:
            log.warning("artifact catalog unavailable", error_type=type(exc).__name__)
            response.error = "Artifacts 暂时无法读取，请重试"
        await machine.transport.send(response)
        return response

    async def preview(self, cmd):
        machine = self.machine
        path = f"artifacts/{cmd.artifact_id}"
        try:
            sid, cwd, scope = await self.resolve(cmd)
            row = await asyncio.to_thread(self.store.get, scope, cmd.artifact_id)
            if row is None or row["identity"].startswith("missing:"):
                raise ValueError("该文件不属于当前会话")
            path = row["path"]
            if cmd.variant == "thumbnail":
                async with machine._preview_conversion_limit:
                    data = await self.thumbnail(cmd, sid, cwd, row)
                response = FilePreview(path=path, request_id=cmd.request_id, format="image",
                    data=base64.b64encode(data).decode(), media_type="image/webp",
                    size=len(data), writable=False, sid=sid, to=cmd.client_id)
            elif row["image_id"]:
                image = await (machine._dsh.handle_get_history_image if cmd.engine == "dsh" else machine._handle_get_history_image)(GetHistoryImage(
                    session_id=sid, turn_id=row["turn_id"], image_id=row["image_id"],
                    variant="full", request_id=cmd.request_id, client_id=cmd.client_id), publish=False)
                response = FilePreview(path=path, request_id=cmd.request_id, format="image",
                    data=image.data, media_type="image/jpeg" if image.media_type == "image/jpg" else image.media_type,
                    error=image.error, sid=sid, to=cmd.client_id,
                    size=len(base64.b64decode(image.data)) if image.data else 0)
            elif machine._ctx_for(sid) is not None and row["source"] != "upload":
                # Keep the existing file authorization, editor and conversion behavior.
                return await machine._handle_get_file_preview(GetFilePreview(
                    sid=sid, path=path, request_id=cmd.request_id, client_id=cmd.client_id,
                    owner_id=cmd.owner_id))
            else:
                # Immutable upload copies are session-scoped and read-only. Cold
                # Code file reads need no engine resume; Work keeps its cwd boundary.
                caps = {}
                if row["source"] == "upload" or cmd.space == "code":
                    cap = await asyncio.to_thread(machine._preview_capability_store.inspect_path,
                        cmd.engine, cmd.space, sid, path, mode="read", source="user_approved")
                    caps[cap.path] = cap
                if machine._preview_format(path) == "video":
                    data = await asyncio.to_thread(machine._video_file_preview, cwd, path,
                        cmd.engine, cmd.space, sid, cmd.owner_id, caps)
                else:
                    async with machine._preview_conversion_limit:
                        data = await asyncio.to_thread(machine._read_file_preview, cwd, path, caps)
                response = FilePreview(path=path, request_id=cmd.request_id, format=data["format"],
                    content=data.get("content", ""), media_type=data.get("media_type"),
                    media_id=data.get("media_id"),
                    data=base64.b64encode(data["data"]).decode() if data.get("data") is not None else None,
                    size=data["size"], converted_from=data.get("converted_from"),
                    truncated=data.get("truncated", False), mtime_ns=str(data["mtime_ns"]),
                    writable=False, sid=sid, to=cmd.client_id)
        except Exception as exc:
            log.warning("artifact preview unavailable", error_type=type(exc).__name__)
            response = FilePreview(path=path, request_id=cmd.request_id, sid=cmd.sid,
                error="文件已移动、删除或暂时无法预览，请刷新 Artifacts", to=cmd.client_id)
        await machine.transport.send(response)
        return response

    async def thumbnail(self, cmd, sid, cwd, row):
        machine = self.machine
        if row["image_id"]:
            image = await (machine._dsh.handle_get_history_image if cmd.engine == "dsh" else machine._handle_get_history_image)(GetHistoryImage(
                session_id=sid, turn_id=row["turn_id"], image_id=row["image_id"],
                variant="thumbnail", request_id=cmd.request_id, client_id=cmd.client_id), publish=False)
            if image.error or not image.data:
                raise ValueError("历史缩略图不可用")
            raw = base64.b64decode(image.data, validate=True)
        else:
            suffixes = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"})
            if Path(row["path"]).suffix.lower() not in suffixes:
                raise ValueError("没有可用缩略图")
            # Automatic reads never grant access to files merely mentioned in prose.
            caps = await asyncio.to_thread(machine._preview_capability_store.snapshot,
                cmd.engine, cmd.space, sid)
            if row["source"] == "upload":
                cap = await asyncio.to_thread(machine._preview_capability_store.inspect_path,
                    cmd.engine, cmd.space, sid, row["path"], mode="read", source="user_approved")
                caps[cap.path] = cap
            _, raw, _, _ = await asyncio.to_thread(machine._read_session_file,
                cwd, row["path"], allowed_suffixes=suffixes,
                max_bytes=MAX_SINGLE_ATTACHMENT_BYTES, allow_truncate=False,
                allowed_external_paths=caps)
        return await asyncio.to_thread(image_thumbnail, raw)
