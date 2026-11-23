"""Portable, bounded XLSX and delimited text previews.

Never evaluates formulas, follows external relationships or extracts a ZIP to disk.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import csv
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import cached_property
from io import BytesIO, StringIO
import json
import posixpath
import re
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile

from cc_remote.protocol import ARTIFACT_PREVIEW_MAX_BYTES

# The CSV field limit is shared across readers. Configure it once to match
# the input byte cap so concurrent previews can parse and trim long fields.
csv.field_size_limit(ARTIFACT_PREVIEW_MAX_BYTES)

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
NUMBER_FORMATS = {
    9: "0%", 10: "0.00%", 14: "yyyy-mm-dd", 15: "d-mmm-yy",
    16: "d-mmm", 17: "mmm-yy", 18: "h:mm AM/PM", 19: "h:mm:ss AM/PM",
    20: "h:mm", 21: "h:mm:ss", 22: "yyyy-mm-dd h:mm", 45: "mm:ss",
}
DATE_FORMAT_TOKEN = re.compile(r'"[^"]*"|\\.|AM/PM|y{2,4}|m{1,5}|d{1,4}|h{1,2}|s{1,2}|.', re.I)


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
        options = workbook.find(self.ns + "workbookPr")
        self.date_1904 = options is not None and options.get("date1904") in {"1", "true"}
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

    @cached_property
    def number_formats(self) -> list[str]:
        if not self.has_styles:
            return []
        root = self.xml("xl/styles.xml")
        formats = {str(key): value for key, value in NUMBER_FORMATS.items()}
        formats.update({item.get("numFmtId"): item.get("formatCode", "")[:128]
                        for item in root.findall(f"{self.ns}numFmts/{self.ns}numFmt")})
        return [formats.get(item.get("numFmtId"), "")
                for item in root.findall(f"{self.ns}cellXfs/{self.ns}xf")]

    def display_value(self, cell: ET.Element, value: str) -> str:
        if not value or cell.get("t") not in {None, "n"}:
            return value
        style = cell.get("s", "0")
        if not re.fullmatch(r"[0-9]{1,7}", style) or int(style) >= len(self.number_formats):
            return value
        code = self.number_formats[int(style)]
        if (not code or len(value) > 64
                or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value)):
            return value
        percent = re.fullmatch(r"[#0]+(?:\.(0{1,10}))?%", code)
        try:
            number = Decimal(value)
            if percent and abs(number) < Decimal("1e20"):
                places = len(percent[1] or "")
                rounded = number.scaleb(2).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
                return f"{rounded:.{places}f}%"
            if not 0 <= number < 3_000_000:
                return value
            return _format_date(number, code, self.date_1904) or value
        except (InvalidOperation, OverflowError, ValueError):
            return value


def _format_date(number: Decimal, code: str, date_1904: bool) -> str | None:
    # Keep unsupported custom format syntax as the original saved value.
    if not re.fullmatch(r"""(?:"[^"]*"|\\.|AM/PM|y{2,4}|m{1,5}|d{1,4}|h{1,2}|s{1,2}|[-/.: ,])+""", code, re.I):
        return None
    tokens = DATE_FORMAT_TOKEN.findall(code)
    if not any(token.lower()[0] in "ymdhs" for token in tokens):
        return None
    day = int(number)
    seconds = int(((number - day) * 86400).to_integral_value(rounding=ROUND_HALF_UP))
    epoch = datetime(1904, 1, 1) if date_1904 else datetime(1899, 12, 31)
    date = epoch + timedelta(days=day - (not date_1904 and day >= 60), seconds=seconds)
    fake_leap_day = not date_1904 and day == 60 and seconds < 86400
    month_names = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    week_names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    fields = [token.lower() for token in tokens if token.lower()[0] in "ymdhs"]
    clock_12 = any(token.lower() == "am/pm" for token in tokens)
    parts = []
    index = 0
    for token in tokens:
        lower = token.lower()
        if lower == "am/pm":
            parts.append("AM" if date.hour < 12 else "PM")
            continue
        if token[0] in {'"', "\\"}:
            parts.append(token[1:-1] if token[0] == '"' else token[1:])
            continue
        symbol, width = lower[0], len(lower)
        if symbol not in "ymdhs":
            parts.append(token)
            continue
        if symbol == "y":
            parts.append(f"{date.year % 100:02d}" if width == 2 else str(date.year))
        elif symbol == "m" and not (index and fields[index - 1].startswith("h")
                                    or index + 1 < len(fields) and fields[index + 1].startswith("s")):
            name = month_names[date.month - 1]
            parts.append(name if width == 3 else name[0] if width == 5 else
                         date.strftime("%B") if width == 4 else f"{date.month:0{width}d}")
        elif symbol == "d" and width >= 3:
            parts.append(week_names[date.weekday()] if width == 3 else date.strftime("%A"))
        else:
            field = {"m": date.minute, "d": 29 if fake_leap_day else date.day, "s": date.second,
                     "h": date.hour % 12 or 12 if clock_12 else date.hour}[symbol]
            parts.append(f"{field:0{width}d}")
        index += 1
    return "".join(parts)


@contextmanager
def open_workbook(data: bytes) -> Iterator[Workbook]:
    if len(data) > ARTIFACT_PREVIEW_MAX_BYTES:
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
                displayed = workbook.display_value(cell, value)
                item = {"r": row_number, "c": column, "v": displayed[:MAX_TEXT]}
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


def delimited_preview(content: str, suffix: str, prefix_only: bool) -> str:
    cells = []
    trimmed = prefix_only
    budget = MAX_OUTPUT_BYTES - 2048
    reader = csv.reader(StringIO(content, newline=""), strict=True,
                        delimiter="," if suffix == ".csv" else "\t")
    last_complete_line = None
    if prefix_only and not content.endswith(("\n", "\r")):
        last_complete_line = content.count("\n") + content.count("\r") - content.count("\r\n")
    try:
        for row, values in enumerate(reader, 1):
            if row > 500 or (last_complete_line is not None and reader.line_num > last_complete_line):
                trimmed = True
                break
            if len(values) > 100:
                trimmed = True
            for column, value in enumerate(values[:100], 1):
                if not value:
                    continue
                item = {"r": row, "c": column, "v": value[:MAX_TEXT]}
                length = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                if len(cells) >= MAX_CELLS or length + 1 > budget:
                    trimmed = True
                    break
                cells.append(item)
                budget -= length + 1
                trimmed |= len(value) > MAX_TEXT
    except csv.Error as exc:
        if not prefix_only:
            raise ValueError("无法读取此 CSV 或 TSV 文件") from exc
    return json.dumps({"sheets": [{"name": suffix[1:].upper(), "cells": cells,
                                  "truncated": trimmed}], "truncated": False},
                      ensure_ascii=False, separators=(",", ":"))
