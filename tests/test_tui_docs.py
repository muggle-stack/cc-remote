"""Keep translated setup examples usable with the actual shortcut parser."""

from pathlib import Path
import re
import tomllib

import pytest

from cc_remote.tui_keys import KeyConfig


@pytest.mark.parametrize("name", ["tui.md", "tui_zh.md"])
def test_guide_key_examples_have_no_binding_conflicts(name):
    root = Path(__file__).resolve().parents[1]
    source = (root / "docs" / name).read_text()
    examples = re.findall(r"```toml\n(.*?)\n```", source, re.DOTALL)
    assert examples
    for example in examples:
        assert KeyConfig(tomllib.loads(example)).help()


@pytest.mark.parametrize(
    ("readme", "guide"),
    [("README.md", "tui_zh.md"), ("README_en.md", "tui.md")],
)
def test_readme_points_to_the_current_session_tree(readme, guide):
    root = Path(__file__).resolve().parents[1]
    source = (root / readme).read_text()
    assert f"(docs/{guide})" in source
    assert (root / "docs" / guide).is_file()
    assert "Space e" in source
    assert "Ctrl+p" not in source
