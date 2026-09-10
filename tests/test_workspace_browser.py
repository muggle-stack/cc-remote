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


def test_private_directory_read_wire_round_trip():
    command = BrowseFiles(sid="profile@thread", request_id="read", path="src", hidden=True)
    assert deserialize(serialize(command)) == command
    response = FilesListed(sid="profile@thread", to="browser", request_id="read", root="/project", path="/project/src")
    assert deserialize(serialize(response)) == response
