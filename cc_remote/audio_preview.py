"""Container checks for bounded, read-only audio artifact previews.

These checks identify the container, not codec support. The browser decodes the
original bytes and can offer a download when its audio decoder rejects them.
"""
from __future__ import annotations

from typing import BinaryIO

AUDIO_PREVIEW_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".wave": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
}


def validate_audio_preview(media_type: str, data: bytes) -> None:
    valid = {
        "audio/wav": (len(data) >= 12 and data[:4] in {b"RIFF", b"RIFX", b"RF64"}
                      and data[8:12] == b"WAVE"),
        "audio/mpeg": (data.startswith(b"ID3") or (
            len(data) >= 4 and data[0] == 0xff and data[1] & 0xe0 == 0xe0
            and data[1] & 0x18 != 0x08 and data[1] & 0x06 != 0
            and data[2] & 0xf0 != 0xf0 and data[2] & 0x0c != 0x0c)),
        "audio/mp4": len(data) >= 16 and data[4:8] == b"ftyp",
        "audio/aac": len(data) >= 7 and data[0] == 0xff and data[1] & 0xf6 == 0xf0,
        "audio/flac": data.startswith(b"fLaC"),
        "audio/ogg": len(data) >= 27 and data[:5] == b"OggS\0",
        "audio/webm": data.startswith(b"\x1a\x45\xdf\xa3"),
    }.get(media_type, False)
    if not valid:
        raise ValueError("文件内容与音频格式不匹配")


def webm_media_type(stream: BinaryIO, file_size: int) -> str:
    """Read bounded EBML track metadata without decoding media packets."""
    def vint(*, identifier: bool = False) -> tuple[int, bool]:
        first = stream.read(1)
        width = 9 - first[0].bit_length() if first else 9
        if width > (4 if identifier else 8):
            raise ValueError("WebM 容器标头无效")
        tail = stream.read(width - 1)
        if len(tail) != width - 1:
            raise ValueError("WebM 容器标头不完整")
        value = int.from_bytes(first + tail, "big")
        if identifier:
            return value, False
        mask = (1 << (7 * width)) - 1
        return value & mask, (value & mask) == mask

    # Tracks normally precede the first Cluster. Skip sized elements using
    # seek, and bound the work even for files with many tiny elements.
    remaining = 4096

    def elements(end: int, *, segment_allowed: bool = False):
        nonlocal remaining
        while stream.tell() < end:
            remaining -= 1
            if remaining < 0:
                raise ValueError("WebM 轨道标头超过读取限制")
            kind, _ = vint(identifier=True)
            length, unknown = vint()
            start = stream.tell()
            if unknown and not (segment_allowed and kind == 0x18538067):
                raise ValueError("WebM 容器元素大小无效")
            stop = end if unknown else start + length
            if not start <= stop <= end:
                raise ValueError("WebM 容器元素大小无效")
            yield kind, stop
            stream.seek(stop)

    stream.seek(0)
    roots = elements(file_size, segment_allowed=True)
    header = next(roots, None)
    if header is None or header[0] != 0x1A45DFA3:
        raise ValueError("该文件不是有效的 WebM 媒体")
    doc_type = next((stream.read(4) for kind, end in elements(header[1])
                     if kind == 0x4282 and end - stream.tell() == 4), None)
    if doc_type != b"webm":
        raise ValueError("该文件不是有效的 WebM 媒体")
    has_audio = False
    for kind, end in roots:
        if kind != 0x18538067:
            continue
        for section, limit in elements(end):
            if section != 0x1654AE6B:
                continue
            for entry, track_end in elements(limit):
                if entry != 0xAE:
                    continue
                for field, field_end in elements(track_end):
                    if field == 0x83 and 1 <= field_end - stream.tell() <= 8:
                        track_type = int.from_bytes(stream.read(field_end - stream.tell()), "big")
                        if track_type == 1:
                            return "video/webm"
                        if track_type == 2:
                            has_audio = True
            if has_audio:
                return "audio/webm"
    raise ValueError("WebM 文件没有可预览的音视频轨道")
