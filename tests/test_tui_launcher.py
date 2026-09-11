"""A linked launcher always imports its own checkout, not the caller's cwd."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest


def test_launcher_uses_own_checkout_from_unrelated_cwd(tmp_path):
    root = Path(__file__).resolve().parents[1]
    link = tmp_path / "tui"
    link.symlink_to(root / "scripts/cc-remote-tui")
    result = subprocess.run(
        ["bash", str(link), "--list-keys", "normal.tree", "--json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=10, check=True,
    )
    assert json.loads(result.stdout)[0]["id"] == "normal.tree"
    assert "password" not in result.stdout.lower()


@pytest.mark.parametrize("relative", [False, True])
def test_launcher_with_bsd_readlink_and_chained_symlinks(tmp_path, relative):
    root = Path(__file__).resolve().parents[1]
    bindir = tmp_path / "bin with spaces"
    bindir.mkdir()
    # Emulate BSD readlink: only the single path operand is accepted.
    readlink = bindir / "readlink"
    readlink.write_text(
        '#!/bin/sh\n[ "$#" -eq 1 ] || exit 64\n'
        f'exec {shlex.quote(sys.executable)} -c '
        "'import os, sys; print(os.readlink(sys.argv[1]))' \"$1\"\n"
    )
    readlink.chmod(0o755)
    inner = bindir / "inner"
    inner.symlink_to(root / "scripts/cc-remote-tui")
    outer = tmp_path / "linked tui"
    outer.symlink_to("bin with spaces/inner" if relative else inner)
    cwd = tmp_path / "unrelated"
    cwd.mkdir()
    result = subprocess.run(
        ["bash", str(outer), "--list-keys", "normal.tree", "--json"],
        cwd=cwd, env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
        capture_output=True, text=True, timeout=10, check=True,
    )
    assert json.loads(result.stdout)[0]["id"] == "normal.tree"
