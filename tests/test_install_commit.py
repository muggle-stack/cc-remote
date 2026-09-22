"""Exercise real Wrapper finalization and cleanup without starting services."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("previous_install", [False, True])
@pytest.mark.parametrize("phase", ["interrupt-readiness", "success", "fail-output"])
def test_wrapper_registration_matches_the_activation_commit(tmp_path, previous_install, phase):
    root = tmp_path / "managed installation"
    target = root / "releases/new"
    previous = root / "releases/old"
    (target / ".venv/bin").mkdir(parents=True)
    (target / ".venv/bin/python").symlink_to(sys.executable)
    (target / "deploy").mkdir()
    for name in ("install_cli.py", "atomic_symlink.py"):
        shutil.copyfile(ROOT / "deploy" / name, target / "deploy" / name)
    (target / "deploy/check_codex_readiness.py").write_text(
        "import os, signal\n"
        "if os.environ['TEST_PHASE'] == 'interrupt-readiness':\n"
        "    assert os.getppid() == int(os.environ['TEST_INSTALLER_PID'])\n"
        "    os.kill(os.getppid(), signal.SIGTERM)\n"
    )
    (target / "bin").mkdir()
    launcher = (ROOT / "scripts/cc-remote").read_bytes()
    (target / "bin/cc-remote").write_bytes(launcher)
    current = root / "current"
    current.symlink_to(target)
    cli = tmp_path / "bin/cc-remote"
    metadata = root / "installation.json"
    old_metadata = b'{"schema":1,"role":"wrapper","user":"prior-user"}\n'
    old_launcher = launcher + b"\n# previous launcher\n"
    if previous_install:
        previous.mkdir()
        cli.parent.mkdir()
        cli.write_bytes(old_launcher)
        cli.chmod(0o755)
        metadata.write_bytes(old_metadata)

    settings = {
        "system": "darwin", "appdir": str(root), "target": str(target),
        "previous": str(previous) if previous_install else "", "current": str(current),
        "cli_path": str(cli), "target_user": "fixture-user", "target_home": str(tmp_path),
        "service_file": str(tmp_path / "wrapper.plist"), "service_label": "fixture-wrapper",
        "config_dir": str(tmp_path / "config"), "log_dir": str(tmp_path / "logs"),
        "activation_started": "0", "version": "4.0.1", "git_sha": "a" * 40,
        "stage": "", "service_backup": "", "device_backup": "", "unit_verify_dir": "",
        "rollback_snapshot": "", "snapshot_created": "0", "service_changed": "0",
        "device_changed": "0", "service_stopped": "0", "service_was_running": "0",
        "switched": "1", "activation_committed": "0",
    }
    source = (ROOT / "deploy/install-wrapper.sh").read_text()
    helpers = source[source.index("restart_after_rollback() {"):]
    helpers = helpers.split('if [ -e "$service_file" ]; then', 1)[0]
    finalization = source[source.index("# The real Wrapper prepares"):]
    harness = "set -euo pipefail\n" + "\n".join(
        f"{key}={shlex.quote(value)}" for key, value in settings.items()
    ) + "\n" + helpers + r'''
export TEST_INSTALLER_PID=$$
launchctl() { return 0; }
echo() {
  if [ "$TEST_PHASE" = fail-output ]; then
    case "$*" in "Wrapper v"*) return 1 ;; esac
  fi
  builtin echo "$@"
}
''' + finalization
    result = subprocess.run(
        ["bash", "-c", harness],
        env={**os.environ, "TEST_PHASE": phase},
        capture_output=True, text=True, timeout=10,
    )
    if phase == "interrupt-readiness":
        assert result.returncode == 130, result.stderr
        assert "activation failed" in result.stderr
        if previous_install:
            assert current.resolve() == previous
            assert cli.read_bytes() == old_launcher
            assert metadata.read_bytes() == old_metadata
        else:
            assert not current.is_symlink()
            assert not cli.exists()
            assert not metadata.exists()
    else:
        assert result.returncode == (1 if phase == "fail-output" else 0), result.stderr
        assert current.resolve() == target
        bound = f"export CC_REMOTE_MANAGED_ROOT={shlex.quote(str(root))}\n".encode()
        assert cli.read_bytes().replace(bound, b"", 1) == launcher
        assert json.loads(metadata.read_text()) == {
            "schema": 1, "role": "wrapper", "user": "fixture-user", "service_label": "fixture-wrapper",
        }
        if phase == "fail-output":
            assert "activation was committed" in result.stderr
            assert "activation failed" not in result.stderr
