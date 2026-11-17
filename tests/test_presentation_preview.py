"""PPTX uses the authenticated file reader without a native Office converter."""
import base64
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from cc_remote.protocol import FilePreview, GetFilePreview, serialize, deserialize
from cc_remote.wrapper.machine import WrapperMachine
from cc_remote.wrapper.presentation_preview import validate_presentation_preview
from tests.test_multisession import _mk_ctx, _mk_machine


def package(parts=None):
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, content in (parts or {
            "[Content_Types].xml": "<Types/>",
            "ppt/presentation.xml": "<presentation/>",
        }).items():
            archive.writestr(name, content)
    return output.getvalue()


@pytest.mark.parametrize("engine", ["claude", "codex", "dsh"])
@pytest.mark.asyncio
async def test_pptx_preview_returns_original_package(tmp_path, monkeypatch, engine):
    data = package()
    (tmp_path / "slides.PPTX").write_bytes(data)
    machine, transport = _mk_machine()
    ctx = _mk_ctx("presentation-session", session_id="presentation-session")
    ctx.cwd = str(tmp_path)
    ctx.engine = engine
    machine.sessions[ctx.key] = ctx
    monkeypatch.setattr("cc_remote.wrapper.machine.shutil.which",
                        lambda _name: pytest.fail("PPTX must not discover Office binaries"))
    response = await machine._handle_get_file_preview(GetFilePreview(
        sid=ctx.key, client_id="reader", path="slides.PPTX", request_id="preview"))
    assert response.error is None
    assert response.format == "presentation"
    assert response.converted_from is None
    assert response.to == "reader"
    assert response.sid == ctx.key
    assert base64.b64decode(response.data) == data
    assert deserialize(serialize(response)) == response
    assert any(isinstance(event, FilePreview) for event in transport.sent)


@pytest.mark.parametrize("data", [
    b"not a presentation",
    package({"ordinary.txt": "zip"}),
    package({"[Content_Types].xml": "<Types/>",
             "ppt/presentation.xml": b"x" * (16 * 1024 * 1024 + 1)}),
])
def test_pptx_rejects_invalid_or_oversized_packages(data):
    with pytest.raises(ValueError):
        validate_presentation_preview(data)


def test_pptx_preserves_file_authorization(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    source = tmp_path / "outside.pptx"
    source.write_bytes(package())
    with pytest.raises(ValueError):
        WrapperMachine._read_file_preview(str(root), str(source))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be"])
def test_pptx_rejects_xml_entities_before_browser_parse(encoding):
    xml = '<!DOCTYPE ppt [<!ENTITY secret "expanded">]><ppt>&secret;</ppt>'
    with pytest.raises(ValueError, match="XML"):
        validate_presentation_preview(package({
            "[Content_Types].xml": "<Types/>",
            "ppt/presentation.xml": xml.encode(encoding),
        }))
