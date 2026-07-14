"""Attachment limits shared by wrapper validation and tests.

The WebSocket frame itself is capped separately. These decoded limits keep a
valid frame from expanding into unbounded temporary files or SDK payloads.
"""
from __future__ import annotations

import base64
import binascii
import struct
from typing import Optional


MAX_ATTACHMENT_COUNT = 8
MAX_SINGLE_ATTACHMENT_BYTES = 6 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8192
MAX_IMAGE_PIXELS = 16_000_000
MAX_TOTAL_IMAGE_PIXELS = 32_000_000
MAX_FILENAME_BYTES = 240
ALLOWED_IMAGE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/jpg", "image/webp",
})


def decode_attachment(data: str) -> bytes:
    """Strictly decode browser-produced base64 without accepting junk suffixes."""
    if not isinstance(data, str) or not data:
        raise ValueError("attachment data is empty")
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("attachment data is not valid base64") from exc


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    pos = 2
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
           0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1
        if marker in {0xD8, 0xD9}:
            continue
        if pos + 2 > len(data):
            break
        length = int.from_bytes(data[pos:pos + 2], "big")
        if length < 2 or pos + length > len(data):
            break
        if marker in sof and length >= 7:
            height = int.from_bytes(data[pos + 3:pos + 5], "big")
            width = int.from_bytes(data[pos + 5:pos + 7], "big")
            return width, height
        pos += length
    return None


def image_dimensions(data: bytes, media_type: str) -> tuple[int, int] | None:
    """Validate declared type against a minimal header and return dimensions."""
    normalized = "image/jpeg" if media_type == "image/jpg" else media_type
    if normalized == "image/png":
        if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            return None
        # APNG is identified by an actual ``acTL`` chunk, not by those four
        # bytes appearing somewhere in compressed pixels or text metadata.
        # Keep accepting the legacy header-only fixtures used by the protocol
        # tests: an incomplete chunk simply ends this best-effort scan.
        pos = 8
        while pos + 8 <= len(data):
            length = int.from_bytes(data[pos:pos + 4], "big")
            kind = data[pos + 4:pos + 8]
            chunk_end = pos + 12 + length
            if chunk_end > len(data):
                break
            if kind == b"acTL":
                return None
            pos = chunk_end
            if kind == b"IEND":
                break
        return struct.unpack(">II", data[16:24])
    if normalized == "image/gif":
        if len(data) < 10 or data[:6] not in {b"GIF87a", b"GIF89a"}:
            return None
        return struct.unpack("<HH", data[6:10])
    if normalized == "image/jpeg":
        return _jpeg_dimensions(data)
    if normalized == "image/webp":
        if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            return None
        kind = data[12:16]
        if kind == b"VP8X":
            if data[20] & 0x02:  # animated WebP
                return None
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return width, height
        if kind == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                    int.from_bytes(data[28:30], "little") & 0x3FFF)
        if kind == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def validate_attachments(
    images: Optional[list[dict[str, str]]],
    files: Optional[list[dict[str, str]]],
) -> Optional[str]:
    """Return a user-facing error string, or ``None`` when the set is safe."""
    images = images or []
    files = files or []
    if len(images) + len(files) > MAX_ATTACHMENT_COUNT:
        return f"too many attachments (max {MAX_ATTACHMENT_COUNT})"

    total = 0
    total_pixels = 0
    for image in images:
        media_type = image.get("media_type", "")
        if media_type not in ALLOWED_IMAGE_TYPES:
            # Never reflect an untrusted, potentially frame-sized MIME string
            # into an Error event and back across the wrapper WebSocket.
            return "unsupported image type"
        try:
            decoded = decode_attachment(image.get("data", ""))
            size = len(decoded)
        except ValueError as exc:
            return str(exc)
        if size > MAX_SINGLE_ATTACHMENT_BYTES:
            return "one attachment exceeds the 6 MiB limit"
        total += size
        dimensions = image_dimensions(decoded, media_type)
        if dimensions is None:
            return "image content does not match its declared type"
        width, height = dimensions
        if (
            width <= 0 or height <= 0
            or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION
            or width * height > MAX_IMAGE_PIXELS
        ):
            return "image dimensions exceed the safe limit"
        total_pixels += width * height
        if total_pixels > MAX_TOTAL_IMAGE_PIXELS:
            return "combined image dimensions exceed the safe limit"

    for file in files:
        filename = file.get("filename", "")
        try:
            filename_size = (
                len(filename.encode("utf-8")) if isinstance(filename, str) else 0
            )
        except UnicodeEncodeError:
            filename_size = 0
        if not filename or not filename_size or filename_size > MAX_FILENAME_BYTES:
            return "attachment filename is missing or too long"
        try:
            size = len(decode_attachment(file.get("data", "")))
        except ValueError as exc:
            return str(exc)
        if size > MAX_SINGLE_ATTACHMENT_BYTES:
            return "one attachment exceeds the 6 MiB limit"
        total += size

    if total > MAX_TOTAL_ATTACHMENT_BYTES:
        return "attachments exceed the 8 MiB total limit"
    return None
