"""Container checks for bounded, read-only audio artifact previews.

These checks identify the container, not codec support. The browser decodes the
original bytes and can offer a download when its audio decoder rejects them.
"""
from __future__ import annotations

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
