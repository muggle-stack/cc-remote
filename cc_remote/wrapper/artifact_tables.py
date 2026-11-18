"""Read one table region as source values or as a static preview grid."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from html import escape
from io import StringIO
from itertools import islice
import json
import re
import xml.etree.ElementTree as ET

from cc_remote.wrapper.spreadsheet_preview import MAX_TEXT, Workbook, cell_coordinates, open_workbook

MAX_ROWS = 32
MAX_COLUMNS = 12
_MAX_DATA_BYTES = 64 * 1024
_DEFAULT_ROWS = 16
_DEFAULT_COLUMNS = 8


def _column_name(column: int) -> str:
    name = ""
    while column:
        column, index = divmod(column - 1, 26)
        name = chr(65 + index) + name
    return name


@dataclass(frozen=True)
class Region:
    top: int
    left: int
    bottom: int
    right: int

    @property
    def address(self) -> str:
        start = f"{_column_name(self.left)}{self.top}"
        end = f"{_column_name(self.right)}{self.bottom}"
        return start if start == end else f"{start}:{end}"

    def includes(self, row: int, column: int) -> bool:
        return self.top <= row <= self.bottom and self.left <= column <= self.right


def _region(address: str) -> Region:
    start, separator, end = address.upper().replace("$", "").partition(":")
    first = cell_coordinates(start)
    last = cell_coordinates(end) if separator else first
    if (not first or not last or not first[0] <= last[0] <= 1_048_576
            or not first[1] <= last[1] <= 16_384):
        raise ValueError("Use a valid A1 range, for example B2:F12.")
    return Region(*first, *last)


@dataclass(frozen=True)
class Cell:
    value: str
    formula: str | None = None
    style: str = ""


@dataclass
class TablePreview:
    title: str
    region: Region
    cells: dict[tuple[int, int], Cell]
    sheets: list[str]
    merged: list[Region]

    def as_json(self) -> str:
        cells = {}
        for (row, column), cell in sorted(self.cells.items()):
            if cell.value or cell.formula:
                item = {"value": cell.value}
                if cell.formula:
                    item["formula"] = cell.formula
                cells[f"{_column_name(column)}{row}"] = item
        text = json.dumps(
            {"sheet": self.title, "sheets": self.sheets, "range": self.region.address, "cells": cells},
            ensure_ascii=False, separators=(",", ":"),
        )
        if len(text.encode("utf-8")) > _MAX_DATA_BYTES:
            raise ValueError("The data exceeds 64 KiB. Select a smaller range.")
        return text

    def as_html(self) -> bytes:
        region = self.region
        columns = range(region.left, region.right + 1)
        header = "".join(f"<th>{_column_name(column)}</th>" for column in columns)
        rows = []
        merged_anchors = {(area.top, area.left): area for area in self.merged}
        covered = {
            (row, column)
            for area in self.merged
            for row in range(area.top, area.bottom + 1)
            for column in range(area.left, area.right + 1)
            if (row, column) != (area.top, area.left)
        }
        for row in range(region.top, region.bottom + 1):
            items = [f"<th>{row}</th>"]
            for column in columns:
                key = row, column
                if key in covered:
                    continue
                cell = self.cells.get(key, Cell(""))
                value = cell.value or ("=" + cell.formula if cell.formula else "")
                span = ""
                if area := merged_anchors.get(key):
                    span = f' rowspan="{area.bottom - row + 1}" colspan="{area.right - column + 1}"'
                items.append(f'<td{span} style="{escape(cell.style)}">{escape(value[:MAX_TEXT])}</td>')
            rows.append("<tr>" + "".join(items) + "</tr>")
        return ("""<!doctype html><meta charset="utf-8"><style>
html { background:#fff; color:#1b2029; font:14px system-ui,sans-serif }
body { margin:0 }
#capture { box-sizing:border-box; max-width:100vw; padding:16px 18px }
h1 { margin:0 0 12px; font-size:18px }
h1 span { margin-left:.8em; color:#565e6a; font-size:14px; font-weight:400 }
table { width:100%; border-collapse:collapse; table-layout:fixed }
th,td { box-sizing:border-box; border:1px solid #d8dde2; height:25px; padding:0 6px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
th { background:#f0f2f5; color:#45505d; font-weight:600 }
th:first-child { width:64px }
</style>"""
                f'<main id="capture" style="width:{100 + len(columns) * 108}px">'
                f"<h1>{escape(self.title)}<span>{region.address}</span></h1>"
                f"<table><thead><tr><th></th>{header}</tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table></main>").encode("utf-8")


def _default_region(dimension: str) -> Region:
    try:
        used = _region(dimension)
    except ValueError:
        return Region(1, 1, _DEFAULT_ROWS, _DEFAULT_COLUMNS)
    return Region(used.top, used.left, min(used.bottom, used.top + _DEFAULT_ROWS - 1),
                  min(used.right, used.left + _DEFAULT_COLUMNS - 1))


def _node_at(nodes: list[ET.Element], index: str) -> ET.Element | None:
    if len(index) <= 7 and index.isascii() and index.isdigit() and int(index) < len(nodes):
        return nodes[int(index)]
    return None


def _rgb(element: ET.Element | None) -> str | None:
    if element is not None and re.fullmatch("[0-9a-fA-F]{8}", value := element.get("rgb", "")):
        return "#" + value[2:]
    return None


def _styles(workbook: Workbook) -> list[str]:
    if not workbook.has_styles:
        return []
    ns = workbook.ns
    root = workbook.xml("xl/styles.xml")
    fonts = root.findall(f"{ns}fonts/{ns}font")
    fills = root.findall(f"{ns}fills/{ns}fill")
    styles = []
    for xf in root.findall(f"{ns}cellXfs/{ns}xf")[:65_536]:
        rules = []
        font = _node_at(fonts, xf.get("fontId", "0"))
        if font is not None:
            for tag, rule in (("b", "font-weight:700"), ("i", "font-style:italic")):
                prop = font.find(ns + tag)
                if prop is not None and prop.get("val") not in ("0", "false"):
                    rules.append(rule)
            if color := _rgb(font.find(ns + "color")):
                rules.append(f"color:{color}")
        fill = _node_at(fills, xf.get("fillId", "0"))
        if fill is not None:
            pattern = fill.find(ns + "patternFill")
            if pattern is not None and pattern.get("patternType") == "solid":
                if color := _rgb(pattern.find(ns + "fgColor")):
                    rules.append(f"background-color:{color}")
        alignment = xf.find(ns + "alignment")
        if alignment is not None and (horizontal := alignment.get("horizontal")) in {"left", "center", "right"}:
            rules.append(f"text-align:{horizontal}")
        styles.append(";".join(rules))
    return styles


def _xlsx_table(data: bytes, sheet: str | None, region: Region | None, visual: bool) -> TablePreview:
    with open_workbook(data) as workbook:
        available = [(name, target) for name, target in workbook.sheets
                     if target and "\\" not in target and ":" not in target]
        if not available:
            raise ValueError("This workbook has no readable worksheets.")
        names = [name for name, _ in available[:64]]
        if sheet is None:
            name, target = available[0]
        else:
            target = next((target for name, target in available if name == sheet), None)
            if target is None:
                raise ValueError(f"Unknown worksheet {sheet!r}. Available sheets: {', '.join(names)}")
            name = sheet
        worksheet = workbook.worksheet(target)
        dimension = worksheet.find(workbook.ns + "dimension")
        region = region or _default_region(dimension.get("ref", "") if dimension is not None else "")
        styles = _styles(workbook) if visual else []
        cells = {}
        for row, column, element in workbook.cells(worksheet):
            if not region.includes(row, column):
                continue
            value, formula = workbook.value(element)
            index = element.get("s", "0")
            style = ""
            if len(index) <= 7 and index.isascii() and index.isdigit() and int(index) < len(styles):
                style = styles[int(index)]
            cells[row, column] = Cell(value, formula, style)
        merged = {}
        if visual:
            for element in worksheet.iterfind(f"{workbook.ns}mergeCells/{workbook.ns}mergeCell"):
                try:
                    area = _region(element.get("ref", ""))
                except ValueError:
                    continue
                if region.includes(area.top, area.left):
                    merged[area.top, area.left] = Region(
                        area.top, area.left, min(region.bottom, area.bottom), min(region.right, area.right),
                    )
        return TablePreview(name, region, cells, names, list(merged.values()))


def _delimited_table(data: bytes, suffix: str, region: Region | None) -> TablePreview:
    explicit = region is not None
    region = region or Region(1, 1, _DEFAULT_ROWS, _DEFAULT_COLUMNS)
    cells = {}
    try:
        reader = csv.reader(StringIO(data.decode("utf-8-sig"), newline=""),
                            delimiter="\t" if suffix == ".tsv" else ",", strict=True)
        for row, fields in enumerate(islice(reader, region.top - 1, region.bottom), region.top):
            for column, value in enumerate(fields[region.left - 1:region.right], region.left):
                cells[row, column] = Cell(value)
    except (csv.Error, UnicodeDecodeError) as exc:
        raise ValueError("This file is not a readable UTF-8 CSV or TSV table.") from exc
    if not explicit and cells:
        region = Region(1, 1, max(row for row, _ in cells), max(column for _, column in cells))
    return TablePreview(suffix[1:].upper(), region, cells, [], [])


def inspect_table(
    data: bytes, suffix: str, sheet: str | None, cell_range: str | None, *, visual: bool,
) -> TablePreview:
    region = _region(cell_range) if cell_range is not None else None
    if region is not None and (region.bottom - region.top >= MAX_ROWS
                               or region.right - region.left >= MAX_COLUMNS):
        raise ValueError(f"Select at most {MAX_ROWS} rows and {MAX_COLUMNS} columns at a time.")
    if suffix == ".xlsx":
        return _xlsx_table(data, sheet, region, visual)
    if sheet is not None:
        raise ValueError("Only XLSX files have worksheet names.")
    return _delimited_table(data, suffix, region)
