import asyncio
import os

import pytest

from cc_remote.protocol import BrowseFiles, FilesListed, deserialize, serialize
from cc_remote.wrapper.workspace_browser import browse_workspace


def test_directory_navigation_paging_and_hidden_files(tmp_path):
    (tmp_path / "folder").mkdir()
    (tmp_path / "z.txt").write_text("z")
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / ".hidden").write_text("private")
    first = browse_workspace(str(tmp_path), ".", limit=2)
    assert first["parent"] is None
    assert [entry["name"] for entry in first["entries"]] == ["folder", "a.txt"]
    second = browse_workspace(str(tmp_path), ".", offset=2, revision=first["revision"])
    assert [entry["name"] for entry in second["entries"]] == ["z.txt"]
    assert second["next_offset"] is None
    assert len(browse_workspace(str(tmp_path), ".", hidden=True)["entries"]) == 4
    child = browse_workspace(str(tmp_path), "folder")
    assert child["parent"] == str(tmp_path)
    file = browse_workspace(str(tmp_path), "a.txt")
    assert file["kind"] == "file" and file["path"] == str(tmp_path / "a.txt")


def test_escape_symlinks_special_files_and_changed_page_are_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("outside")
    (root / "link").symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(root / "pipe")
    for path in ("..", "../outside.txt", "link", "link/outside.txt", "pipe"):
        with pytest.raises(ValueError):
            browse_workspace(str(root), path)
    listed = browse_workspace(str(root), ".")
    assert all(entry["kind"] == "unsupported" for entry in listed["entries"])
    with pytest.raises(ValueError, match="目录已变化"):
        browse_workspace(str(root), ".", offset=1, revision="old")


def test_code_can_browse_parents_absolute_paths_home_and_external_files(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    monkeypatch.setenv("HOME", str(tmp_path))
    start = browse_workspace(str(root), ".", confine_to_cwd=False)
    assert start["root"] == os.path.abspath(os.sep)
    assert start["path"] == str(root)
    assert start["parent"] == str(tmp_path)
    for path in ("..", str(tmp_path), "~"):
        parent = browse_workspace(str(root), path, confine_to_cwd=False)
        assert parent["path"] == str(tmp_path)
        assert {entry["name"] for entry in parent["entries"]} == {"project", "outside.txt"}
    file = browse_workspace(str(root), str(outside), confine_to_cwd=False)
    assert file["kind"] == "file" and file["path"] == str(outside)
    assert "content" not in file  # File content still uses the preview authorization flow.
    top = browse_workspace(str(root), os.path.abspath(os.sep), confine_to_cwd=False)
    assert top["parent"] is None


def test_code_external_browsing_still_rejects_symlinks_and_special_files(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "link").symlink_to(root, target_is_directory=True)
    os.mkfifo(tmp_path / "pipe")
    for path in ("../link", "../link/child", "../pipe"):
        with pytest.raises(ValueError, match="符号链接或特殊文件"):
            browse_workspace(str(root), path, confine_to_cwd=False)


@pytest.mark.parametrize("space", ["code", "work"])
def test_directory_handler_allows_code_parent_but_keeps_work_boundary(tmp_path, space):
    from tests.test_multisession import _mk_ctx, _mk_machine

    root = tmp_path / "project"
    root.mkdir()

    async def scenario():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread", session_id="thread")
        ctx.cwd = str(root)
        ctx.space = space
        machine.sessions[ctx.key] = ctx
        response = await machine._handle_browse_files(BrowseFiles(
            sid=ctx.key, client_id="browser", request_id="parent", path=str(tmp_path)))
        assert response.sid == ctx.key and response.to == "browser"
        assert transport.sent[-1] == response
        if space == "code":
            assert response.error is None and response.path == str(tmp_path)
            assert [entry.name for entry in response.entries] == ["project"]
        else:
            assert "当前会话目录内" in response.error
            assert not response.entries

    asyncio.run(scenario())


def test_private_directory_read_wire_round_trip():
    command = BrowseFiles(sid="profile@thread", request_id="read", path="src", hidden=True)
    assert deserialize(serialize(command)) == command
    response = FilesListed(sid="profile@thread", to="browser", request_id="read", root="/project", path="/project/src")
    assert deserialize(serialize(response)) == response
