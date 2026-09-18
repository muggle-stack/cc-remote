"""Validate service syntax with the system manager that consumes it."""

import shutil
import subprocess
import sys

import pytest

from deploy.install_claude_service import unit_text


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("systemd-analyze"),
                    reason="systemd unit verification requires Linux")
def test_service_unit_with_spaces_is_accepted_by_systemd(tmp_path):
    source = tmp_path / "release with spaces"
    source.mkdir()
    state = tmp_path / "state with spaces"
    state.mkdir()
    unit = tmp_path / "claude-test.service"
    unit.write_text(unit_text(source, state))
    result = subprocess.run(["systemd-analyze", "verify", str(unit)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
