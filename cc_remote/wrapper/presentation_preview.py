"""Validate bounded PPTX packages before sending original bytes to the browser."""
from __future__ import annotations

from io import BytesIO
from zipfile import BadZipFile, ZipFile

from cc_remote.protocol import ARTIFACT_PREVIEW_MAX_BYTES

MAX_PART_BYTES = 16 * 1024 * 1024
MAX_INFLATED_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 4000


def validate_presentation_preview(data: bytes) -> None:
    if len(data) > ARTIFACT_PREVIEW_MAX_BYTES:
        raise ValueError("PPTX 超过 8 MiB 预览上限")
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if not {"[Content_Types].xml", "ppt/presentation.xml"} <= names:
                raise ValueError("文件不是有效的 PPTX 演示文稿")
            if len(entries) > MAX_ENTRIES or len(names) != len(entries):
                raise ValueError("PPTX 条目过多或包含重复条目")
            if any(entry.flag_bits & 1 for entry in entries):
                raise ValueError("暂不支持加密的 PPTX")
            if (any(entry.file_size > MAX_PART_BYTES for entry in entries)
                    or sum(entry.file_size for entry in entries) > MAX_INFLATED_BYTES):
                raise ValueError("PPTX 解压后超过预览上限")
            for entry in entries:
                if entry.filename.lower().endswith((".xml", ".rels")):
                    xml = archive.read(entry).replace(b"\0", b"").upper()
                    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
                        raise ValueError("PPTX 包含不支持的 XML 文档声明")
    except BadZipFile as exc:
        raise ValueError("PPTX 无法解析或已损坏") from exc
