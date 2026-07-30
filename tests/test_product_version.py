from __future__ import annotations

import json
import re
from pathlib import Path

from cc_remote import __version__
from cc_remote.protocol import PROTOCOL_VERSION
from cc_remote.wrapper.codex_handle import _initialize_params


ROOT = Path(__file__).resolve().parents[1]


def test_v3_product_version_is_consistent_across_runtime_and_web_metadata():
    assert __version__ == "3.0.0"
    assert re.fullmatch(r"[1-9]\d*\.\d+\.\d+", __version__)

    package = json.loads((ROOT / "web/package.json").read_text())
    package_lock = json.loads((ROOT / "web/package-lock.json").read_text())
    build_manifest = json.loads(
        (ROOT / "web/public/cc-remote-build.json").read_text()
    )

    assert package["version"] == __version__
    assert package_lock["version"] == __version__
    assert package_lock["packages"][""]["version"] == __version__
    assert build_manifest == {
        "version": __version__,
        "protocol": PROTOCOL_VERSION,
    }
    assert _initialize_params()["clientInfo"]["version"] == __version__
    installer = (ROOT / "deploy/install.sh").read_text()
    assert f'VERSION="${{CC_REMOTE_VERSION:-{__version__}}}"' in installer


def test_release_docs_distinguish_product_and_wire_protocol_versions():
    readme = (ROOT / "README.md").read_text()
    readme_en = (ROOT / "README_en.md").read_text()
    changelog = (ROOT / "CHANGELOG.md").read_text()

    assert "当前版本：v3.0.0" in readme
    assert "## v3 架构升级" in readme
    assert "Current release: v3.0.0" in readme_en
    assert "## What changed in v3" in readme_en
    for document in (readme, readme_en, changelog):
        assert "v3.0.0" in document
        assert "protocol v27" in document.lower()


def test_readmes_use_safe_markdown_for_navigation_and_images():
    readme = (ROOT / "README.md").read_text()
    readme_en = (ROOT / "README_en.md").read_text()

    for document in (readme, readme_en):
        assert '<p align="center">' not in document
        assert "<img " not in document
        assert "<a href=" not in document
        assert document.count("](assets/") == 6

    assert "[English](README_en.md)" in readme
    assert "[中文](README.md)" in readme_en
