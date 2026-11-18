"""Validate bounded Office packages before sending them to the browser."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from cc_remote.protocol import ARTIFACT_PREVIEW_MAX_BYTES

MAX_PART_BYTES = 16 * 1024 * 1024
MAX_INFLATED_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 4000


def validate_presentation_preview(data: bytes) -> None:
    with _open_office_preview(data, "PPTX", "ppt/presentation.xml") as source:
        for entry in source.infolist():
            if entry.filename.lower().endswith((".xml", ".rels")):
                _validate_xml(source.read(entry), "PPTX")


def prepare_word_preview(data: bytes) -> bytes:
    # Rebuild the archive from bounded reads so the browser's ZIP decoder
    # receives entry sizes and checksums generated from the actual bytes.
    with _open_office_preview(data, "DOCX", "word/document.xml") as source, BytesIO() as buffer:
        with ZipFile(buffer, "w", compression=ZIP_DEFLATED, compresslevel=1) as output:
            for entry in source.infolist():
                part = source.read(entry)
                if entry.filename.lower().endswith((".xml", ".rels")):
                    _validate_xml(part, "DOCX")
                output.writestr(entry.filename, part)
        return buffer.getvalue()


def _validate_xml(data: bytes, kind: str) -> None:
    text = data.replace(b"\0", b"").upper()
    if b"<!DOCTYPE" in text or b"<!ENTITY" in text:
        raise ValueError(f"{kind} 包含不支持的 XML 文档声明")


@contextmanager
def _open_office_preview(data: bytes, kind: str, main_part: str) -> Iterator[ZipFile]:
    if len(data) > ARTIFACT_PREVIEW_MAX_BYTES:
        raise ValueError(f"{kind} 超过 8 MiB 预览上限")
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if not {"[Content_Types].xml", main_part} <= names:
                raise ValueError(f"文件不是有效的 {kind} 文档")
            if len(entries) > MAX_ENTRIES or len(names) != len(entries):
                raise ValueError(f"{kind} 条目过多或包含重复条目")
            if any(entry.flag_bits & 1 for entry in entries):
                raise ValueError(f"暂不支持加密的 {kind}")
            if (any(entry.file_size > MAX_PART_BYTES for entry in entries)
                    or sum(entry.file_size for entry in entries) > MAX_INFLATED_BYTES):
                raise ValueError(f"{kind} 解压后超过预览上限")
            yield archive
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise ValueError(f"{kind} 无法解析或已损坏") from exc
