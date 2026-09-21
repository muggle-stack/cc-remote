"""Claude's non-interrupting input and native narrative boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from uuid import NAMESPACE_URL, uuid5

from cc_remote.attachments import validate_attachments
from cc_remote.claude_steering import ClaudeSteerRejected
from cc_remote.log import logger
from cc_remote.protocol import ERR_NOT_STEERABLE, ERR_STEER_UNKNOWN, Error, TurnSteered

log = logger("cc_remote.wrapper.claude_steer")


async def handle(machine, ctx, cmd, reject):
    if ctx.state != "running" or ctx.write_state != "writable":
        return await reject(ERR_NOT_STEERABLE, "当前没有可引导的 Claude 任务。")
    if not cmd.prompt and not cmd.images and not cmd.files:
        return await reject(ERR_NOT_STEERABLE, "消息内容为空。")
    if validate_attachments(cmd.images, cmd.files):
        return await reject(ERR_NOT_STEERABLE, "附件不符合要求，请调整后重试。")
    directory = None
    attempted = False
    native_id = str(uuid5(NAMESPACE_URL, json.dumps(
        [cmd.sid, cmd.client_id, cmd.cmd_id])))
    try:
        prompt = cmd.prompt
        if cmd.files:
            directory = tempfile.mkdtemp(prefix="cc-remote-steer-")
            os.chmod(directory, 0o700)
            prompt = machine._stash_files(prompt, cmd.files, directory, ctx.engine)
        if cmd.images:
            prompt = ([{"type": "text", "text": prompt}] if prompt else []) + [
                {"type": "image", "source": {"type": "base64", **image}}
                for image in cmd.images
            ]
        metadata = {"id": cmd.msg_id, "prompt": cmd.prompt, "images": cmd.images,
                    "files": ([{"filename": f["filename"]} for f in cmd.files]
                              if cmd.files else None),
                    "attachment_dir": directory,
                    "fingerprint": hashlib.sha256(json.dumps(
                        [cmd.msg_id, cmd.prompt, cmd.images, cmd.files],
                        sort_keys=True).encode()).hexdigest()}
        async with ctx.steer_lock:
            if ctx.state != "running" or ctx.write_state != "writable":
                raise ClaudeSteerRejected("Claude is no longer running")
            attempted = True
            if directory:
                ctx.claude_steer_attachment_dirs.append(directory)
            await ctx.sdk.steer(prompt, native_id=native_id, metadata=metadata)
        # The command ACK transfers ownership. The replayed native UserMessage
        # publishes TurnSteered later, after the preceding tool/text finishes.
    except ClaudeSteerRejected as exc:
        attempted = False
        log.info("Claude native input rejected", session_id=ctx.session_id,
                 reason=str(exc))
        return await reject(ERR_NOT_STEERABLE,
                            "Claude 当前无法接收引导，本次未发送；请稍后重试或排队。")
    except Exception:
        if not attempted:
            return await reject(ERR_NOT_STEERABLE, "本次引导未发送，请稍后重试。")
        return await reject(ERR_STEER_UNKNOWN,
                            "Claude 尚未确认本次引导，请等待后续输出，避免重复发送。")
    finally:
        if directory and not attempted:
            if directory in ctx.claude_steer_attachment_dirs:
                ctx.claude_steer_attachment_dirs.remove(directory)
            shutil.rmtree(directory, ignore_errors=True)


async def apply_echo(machine, ctx, message, native_id):
    cancelled = getattr(message, "_cc_steer_cancelled", None)
    metadata = cancelled or getattr(message, "_cc_steer", None)
    directory = metadata.get("attachment_dir") if metadata else None
    if directory and directory not in ctx.claude_steer_attachment_dirs:
        ctx.claude_steer_attachment_dirs.append(directory)
    if cancelled:
        return Error(code=ERR_NOT_STEERABLE, msg_id=cancelled["id"],
                     message="本次引导已取消。")
    if not metadata or not native_id:
        return None
    ctx.active_msg_id = metadata["id"]
    ctx.translator.rebind_turn(ctx.active_msg_id)
    await machine._remember_claude_client_message_id(
        ctx, native_id, emit_binding=False)
    return TurnSteered(msg_id=metadata["id"], turn_id=native_id,
                       prompt=metadata["prompt"], images=metadata.get("images"),
                       files=metadata.get("files"))


def adopt(machine, ctx, metadata):
    """Consume an already accepted input only after its exact native echo."""
    previous = ctx.turn_task

    async def run():
        if previous is not None and not previous.done():
            await asyncio.shield(previous)
        ctx.turn_task = asyncio.current_task()
        ctx.claude_background_followups.pop(metadata.get("background_id"), None)
        origin_key = machine._claude_followup_origin_key(metadata.get("background_origin"))
        if origin_key is not None:
            ctx.claude_background_followups.pop(origin_key, None)
        ctx.active_msg_id = metadata["id"]
        ctx.claude_write_active = True
        ctx.needs_reload = False
        if ctx.state not in {"interrupting", "draining"}:
            await machine._set_state(ctx, "running")
        await machine._run_turn(
            ctx, metadata.get("prompt", ""), _adopt_steer=True,
            _recover_service=ctx.sdk.service_recovery is not None)

    ctx.turn_task = asyncio.create_task(run())


def cleanup(ctx):
    for directory in ctx.claude_steer_attachment_dirs:
        shutil.rmtree(directory, ignore_errors=True)
    ctx.claude_steer_attachment_dirs.clear()
