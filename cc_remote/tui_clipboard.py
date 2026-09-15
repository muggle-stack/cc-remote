"""Explicit, bounded desktop clipboard image reads; no polling or shell."""

import asyncio
import base64
import os
import shutil

from cc_remote.attachments import (
    MAX_SINGLE_ATTACHMENT_BYTES,
    validate_attachments,
)

TIMEOUT = 3
IMAGE_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
}


class NoClipboardImage(ValueError):
    pass


async def read_command(args, limit):
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(TIMEOUT):
            data = bytearray()
            while chunk := await process.stdout.read(
                min(65536, limit + 1 - len(data))
            ):
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError("Clipboard data exceeds the size limit")
            if await process.wait():
                raise ValueError("Desktop clipboard is unavailable")
            return bytes(data)
    except TimeoutError as exc:
        raise ValueError("Desktop clipboard timed out") from exc
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


def providers():
    result = []
    if os.environ.get("WAYLAND_DISPLAY") and (exe := shutil.which("wl-paste")):
        result.append(([exe, "--list-types"], [exe, "--no-newline", "--type"]))
    if os.environ.get("DISPLAY") and (exe := shutil.which("xclip")):
        base = [exe, "-selection", "clipboard", "-out", "-target"]
        result.append(([*base, "TARGETS"], base))
    return result


async def read_clipboard_image():
    choices = providers()
    if not choices:
        raise ValueError(
            "No desktop clipboard: use wl-paste on Wayland or xclip on X11. "
            "SSH needs access to that desktop; alternatively use :image /path"
        )
    failure = None
    for listing, read in choices:
        try:
            offered = (
                (await read_command(listing, 16384))
                .decode("utf-8", errors="replace")
                .splitlines()
            )
        except (OSError, ValueError) as exc:
            failure = exc
            continue
        media_type = next((t for t in IMAGE_TYPES if t in offered), None)
        if not media_type:
            # A working desktop clipboard is authoritative. Do not accidentally
            # attach an older image from another display's clipboard.
            raise NoClipboardImage(
                "No PNG/JPEG/WebP image in clipboard; use :image /path for files"
            )
        raw = await read_command(
            [*read, media_type], MAX_SINGLE_ATTACHMENT_BYTES
        )
        content = {
            "media_type": media_type,
            "data": base64.b64encode(raw).decode("ascii"),
        }
        error = validate_attachments([content], None)
        if error:
            raise ValueError(error)
        return {
            "name": "clipboard." + IMAGE_TYPES[media_type],
            "image": True,
            "content": content,
        }
    raise ValueError(
        "Cannot access the desktop clipboard; use :image /path"
    ) from failure
