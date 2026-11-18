from io import BytesIO
import json
from zipfile import ZipFile, ZIP_DEFLATED

import pytest

from cc_remote.wrapper.artifact_inspection import inspect_artifact
from cc_remote.wrapper.artifact_tables import inspect_table
from cc_remote.wrapper.spreadsheet_preview import spreadsheet_preview
from cc_remote.wrapper.machine import WrapperMachine


def workbook(sheet=None, shared=None):
    target = BytesIO()
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="路线图" r:id="s1"/><sheet name="进展" r:id="s2"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="s1" Type="x/worksheet" Target="worksheets/s1.xml"/><Relationship Id="s2" Type="x/worksheet" Target="worksheets/s2.xml"/></Relationships>')
        archive.writestr("xl/worksheets/s1.xml", sheet or '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><f>SUM(B2:B3)</f><v>42</v></c><c r="C1" t="inlineStr"><is><t>&lt;script&gt;alert(1)&lt;/script&gt;</t></is></c></row></sheetData></worksheet>')
        archive.writestr("xl/worksheets/s2.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>')
        archive.writestr("xl/sharedStrings.xml", shared or '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>本周进展</t></si></sst>')
    return target.getvalue()


def test_cached_values_multiple_sheets_and_formula_are_preserved_without_execution(tmp_path, monkeypatch):
    path = tmp_path / "report.xlsx"
    path.write_bytes(workbook())
    monkeypatch.setattr(WrapperMachine, "_convert_office_preview", lambda *args: pytest.fail("must not launch Office"))
    value = WrapperMachine._read_file_preview(str(tmp_path), str(path))
    assert value["format"] == "spreadsheet" and value["data"] == path.read_bytes()
    book = json.loads(value["content"])
    assert [s["name"] for s in book["sheets"]] == ["路线图", "进展"]
    assert book["sheets"][0]["cells"][0]["v"] == "本周进展"
    assert book["sheets"][0]["cells"][1] == {"r": 1, "c": 2, "v": "42", "f": "SUM(B2:B3)"}
    assert "<script>" in book["sheets"][0]["cells"][2]["v"]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_xml_entities_refused_even_in_utf16(encoding):
    xml = '<?xml version="1.0" encoding="' + encoding + '"?><!DOCTYPE sst [<!ENTITY x "expanded">]><sst>&x;</sst>'
    with pytest.raises(ValueError, match="XML"):
        spreadsheet_preview(workbook(shared=xml.encode(encoding)))


def test_cell_budget_and_truncation_are_explicit():
    sheet = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + ''.join(
        f'<row r="{r}"><c r="A{r}" t="inlineStr"><is><t>{"x" * 5000}</t></is></c></row>' for r in range(1, 1000)) + '</sheetData></worksheet>'
    content = spreadsheet_preview(workbook(sheet=sheet))
    assert len(content.encode()) < 512 * 1024
    value = json.loads(content)
    assert value["sheets"][0]["truncated"] and value["sheets"][1]["name"] == "进展"


def test_not_a_workbook_and_external_file_boundary(tmp_path):
    with pytest.raises(ValueError):
        spreadsheet_preview(b"not zip")
    root = tmp_path / "work"
    root.mkdir()
    outside = tmp_path / "secret.xlsx"
    outside.write_bytes(workbook())
    with pytest.raises(ValueError):
        WrapperMachine._read_file_preview(str(root), str(outside))


@pytest.mark.asyncio
async def test_background_table_can_read_late_rows_and_keep_exact_formula_cache(tmp_path):
    source = tmp_path / "report.xlsx"
    source.write_bytes(workbook(sheet="""<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
        <dimension ref="AB900:AC900"/><sheetData><row r="900">
        <c r="AB900"><f>SUM(C900:AA900)</f><v>0.123456789012345</v></c>
        <c r="AC900"><f>SUM(C901:AA901)</f></c>
        </row></sheetData></worksheet>"""))
    result = await inspect_artifact(
        str(tmp_path), source.name, sheet="路线图", cell_range="$AB$900:AC900", view="data",
    )
    assert len(result) == 1
    assert json.loads(result[0].text) == {
        "sheet": "路线图", "sheets": ["路线图", "进展"], "range": "AB900:AC900",
        "cells": {
            "AB900": {"value": "0.123456789012345", "formula": "SUM(C900:AA900)"},
            "AC900": {"value": "", "formula": "SUM(C901:AA901)"},
        },
    }
    with pytest.raises(ValueError, match="进展"):
        await inspect_artifact(str(tmp_path), source.name, sheet="missing", view="data")
    with pytest.raises(ValueError, match="32 rows"):
        await inspect_artifact(str(tmp_path), source.name, cell_range="A1:D100", view="data")


def test_visual_table_displays_literal_markup_as_text():
    table = inspect_table(workbook(), ".xlsx", "路线图", "A1:C1", visual=True)
    markup = table.as_html().decode()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in markup
    assert "<script>" not in markup
