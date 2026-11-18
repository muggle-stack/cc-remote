"""Portable, bounded XLSX value preview.

Never evaluates formulas, follows external relationships or extracts a ZIP to disk.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
import json
import posixpath
import re
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile

MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_PART_BYTES = 16 * 1024 * 1024
MAX_INFLATED_BYTES = 64 * 1024 * 1024
MAX_CELLS = 10000
MAX_TEXT = 2000
MAX_OUTPUT_BYTES = 500 * 1024
MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
STRICT = "{http://purl.oclc.org/ooxml/spreadsheetml/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
STRICT_REL = "{http://purl.oclc.org/ooxml/officeDocument/relationships}id"
CELL_ADDRESS = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})")


class _NoDoctype(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise ValueError("表格包含不支持的 XML 文档声明")


def cell_coordinates(address: str) -> tuple[int, int] | None:
    match = CELL_ADDRESS.fullmatch(address)
    if not match:
        return None
    column = 0
    for char in match[1]:
        column = column * 26 + ord(char) - 64
    return int(match[2]), column


class Workbook:
    def __init__(self, archive: ZipFile):
        self.archive = archive
        entries = archive.infolist()
        if (len(entries) > 2048 or sum(item.file_size for item in entries) > MAX_INFLATED_BYTES
                or len({item.filename for item in entries}) != len(entries)):
            raise ValueError("表格解压后过大或包含重复条目")
        workbook = self.xml("xl/workbook.xml")
        self.ns = STRICT if workbook.tag.startswith(STRICT) else MAIN
        relationships = {
            r.get("Id"): r.get("Target", "") for r in self.xml("xl/_rels/workbook.xml.rels")
            if r.get("TargetMode") != "External" and r.get("Type", "").endswith("/worksheet")
        }
        self.sheets = [
            (sheet.get("name", "工作表")[:128], relationships.get(sheet.get(REL) or sheet.get(STRICT_REL)))
            for sheet in workbook.findall(f"{self.ns}sheets/{self.ns}sheet")
        ]
        self.strings = []
        self.has_styles = "xl/styles.xml" in archive.namelist()
        if "xl/sharedStrings.xml" in archive.namelist():
            for node in self.xml("xl/sharedStrings.xml"):
                self.strings.append("".join(t.text or "" for t in node.iter(self.ns + "t")))
                if len(self.strings) > 100000:
                    raise ValueError("表格共享文本过多")

    def xml(self, name: str) -> ET.Element:
        info = self.archive.getinfo(name)
        if info.flag_bits & 1 or info.file_size > MAX_PART_BYTES:
            raise ValueError("表格工作表过大或已加密")
        # The doctype hook rejects entity declarations even in UTF-16 XML.
        return ET.fromstring(self.archive.read(info), parser=ET.XMLParser(target=_NoDoctype()))

    def worksheet(self, target: str) -> ET.Element:
        path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
        if not path.startswith("xl/"):
            raise ValueError("工作表路径无效")
        return self.xml(path)

    def cells(self, sheet: ET.Element) -> Iterator[tuple[int, int, ET.Element]]:
        for cell in sheet.iterfind(f"{self.ns}sheetData/{self.ns}row/{self.ns}c"):
            if coordinates := cell_coordinates(cell.get("r", "")):
                yield (*coordinates, cell)

    def value(self, cell: ET.Element) -> tuple[str, str | None]:
        formula = cell.findtext(self.ns + "f")
        value = cell.findtext(self.ns + "v", "")
        kind = cell.get("t")
        if kind == "s":
            index = int(value) if len(value) <= 6 and value.isascii() and value.isdigit() else -1
            value = self.strings[index] if 0 <= index < len(self.strings) else ""
        elif kind == "inlineStr":
            value = "".join(t.text or "" for t in cell.iter(self.ns + "t"))
        elif kind == "b":
            value = "TRUE" if value == "1" else "FALSE"
        return value, formula


@contextmanager
def open_workbook(data: bytes) -> Iterator[Workbook]:
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("表格超过 8 MiB 预览上限")
    try:
        with ZipFile(BytesIO(data)) as archive:
            yield Workbook(archive)
    except (BadZipFile, KeyError, ET.ParseError, RuntimeError, OverflowError) as exc:
        raise ValueError("无法读取此 XLSX 文件，请检查文件是否完整或已加密") from exc


def spreadsheet_preview(data: bytes) -> str:
    with open_workbook(data) as workbook:
        budget = MAX_OUTPUT_BYTES - 2048
        remaining = MAX_CELLS
        result = []
        for name, target in workbook.sheets[:64]:
            if not target or "\\" in target or ":" in target:
                continue
            cells = []
            trimmed = False
            if budget < 500 or remaining <= 0:
                result.append({"name": name, "cells": [], "truncated": True})
                continue
            root = workbook.worksheet(target)
            dimension = root.find(workbook.ns + "dimension")
            for row_number, column, cell in workbook.cells(root):
                if column > 100 or row_number > 500:
                    trimmed = True
                    continue
                value, formula = workbook.value(cell)
                if not value and formula:
                    value = "=" + formula
                if not value:
                    continue
                item = {"r": row_number, "c": column, "v": value[:MAX_TEXT]}
                if formula:
                    item["f"] = formula[:MAX_TEXT]
                encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if len(encoded) + 1 > budget:
                    trimmed = True
                    continue
                if len(value) > MAX_TEXT or formula and len(formula) > MAX_TEXT:
                    trimmed = True
                budget -= len(encoded) + 1
                remaining -= 1
                cells.append(item)
                if remaining <= 0 or budget < 500:
                    trimmed = True
                    break
            result.append({"name": name, "cells": cells, "truncated": trimmed,
                           "range": dimension.get("ref", "")[:64] if dimension is not None else ""})
            budget -= len(name.encode("utf-8")) + 160
        if not result:
            raise ValueError("文件中没有可读取的工作表")
        content = json.dumps({"sheets": result, "truncated": len(workbook.sheets) > 64},
                             ensure_ascii=False, separators=(",", ":"))
        if len(content.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ValueError("表格预览内容过大")
        return content
