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


@pytest.mark.parametrize("shell", ["/bin/bash", "bash"], ids=["system-bash", "path-bash"])
@pytest.mark.parametrize("backend", ["darwin", "systemd", "supervisor", "adopt-supervisor"])
@pytest.mark.parametrize("installation", ["fresh", "upgrade", "same-release"])
def test_wrapper_preflight_preserves_arguments_under_nounset(tmp_path, shell, backend, installation):
    # Run the real post-staging block with inert service/retention helpers.
    # /bin/bash exercises Apple's 3.2 on macOS, even when PATH selects Bash 5.
    tmp_path = tmp_path.resolve()
    root = tmp_path / ("managed installation [test]" if backend == "darwin" else "managed")
    target = root / "releases/new"
    (target / ".venv/bin").mkdir(parents=True)
    (target / ".venv/bin/python").symlink_to(sys.executable)
    (target / "deploy").mkdir()
    shutil.copyfile(ROOT / "deploy/linux_service.py", target / "deploy/linux_service.py")
    recorder = tmp_path / "record.py"
    recorder.write_text(
        "import json, os, sys\n"
        "with open(os.environ['TEST_CALLS'], 'a') as output:\n"
        "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'begin': print('test-generation')\n"
    )
    shutil.copyfile(recorder, target / "deploy/release_retention.py")
    calls = tmp_path / "calls.jsonl"
    env_file = tmp_path / "wrapper.env"
    env_file.touch()
    previous = {"fresh": "", "upgrade": str(root / "releases/old"),
                "same-release": str(target)}[installation]
    supervisor = backend in {"supervisor", "adopt-supervisor"}
    settings = {
        "system": "darwin" if backend == "darwin" else "linux",
        "service_manager": "supervisor" if supervisor else "systemd",
        "appdir": str(root), "target": str(target), "previous": previous,
        "target_home": str(tmp_path), "target_user": "fixture-user",
        "service_label": "fixture-wrapper", "service_file": str(tmp_path / "wrapper.conf"),
        "wrapper_env_file": str(env_file), "device_file": str(tmp_path / "device.json"),
        "supervisor_config": str(tmp_path / "supervisord.conf"),
        "adopt_supervisor": "1" if backend == "adopt-supervisor" else "0",
        "linux_profile": "", "retention_generation": "",
        "test_python": sys.executable, "test_recorder": str(recorder),
    }
    source = (ROOT / "deploy/install-wrapper.sh").read_text()
    block = source.split('  stage=""\nfi\n', 1)[1].split('\nif [ -n "$pair_code" ]; then', 1)[0]
    harness = "set -euo pipefail\n" + "\n".join(
        f"{key}={shlex.quote(value)}" for key, value in settings.items()
    ) + r'''
linux_service() { "$test_python" "$test_recorder" "$@"; }
die() { echo "$*" >&2; exit 1; }
''' + block + '\nprintf "%s\\n" "$retention_generation"\n'
    result = subprocess.run(
        [shell, "-c", harness], capture_output=True, text=True, timeout=10,
        env={**os.environ, "TEST_CALLS": str(calls), "TMPDIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    recorded = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    if supervisor:
        assert recorded.pop(0) == (["preflight", "--adopt"] if backend == "adopt-supervisor"
                                   else ["preflight"])
    if installation == "same-release":
        assert recorded == [] and not result.stdout.strip()
        return
    assert result.stdout.strip() == "test-generation"
    expected = ["begin", "--root", str(root), "--release", str(target),
                "--previous", previous, "--role", "wrapper", "--home", str(tmp_path),
                "--service", "fixture-wrapper", "--service-file", settings["service_file"],
                "--config", settings["device_file"], "--config", str(env_file),
                "--config", str(root / "installation.json")]
    assert len(recorded) == 1
    if supervisor:
        assert recorded[0][-2] == "--linux-service"
        profile = Path(recorded[0][-1])
        assert json.loads(profile.read_text())["manager"] == "supervisor"
        expected.extend(["--linux-service", str(profile)])
    assert recorded[0] == expected


@pytest.mark.parametrize('prior', ['absent', 'stopped', 'running'])
@pytest.mark.parametrize('remove_fails', [False, True])
def test_supervisor_rollback_restores_registration(tmp_path, prior, remove_fails):
    root = tmp_path / 'managed'
    target = root / 'releases/new'
    previous = root / 'releases/old'
    (target / '.venv/bin').mkdir(parents=True)
    (target / '.venv/bin/python').symlink_to(sys.executable)
    (target / 'deploy').mkdir()
    shutil.copyfile(ROOT / 'deploy/atomic_symlink.py', target / 'deploy/atomic_symlink.py')
    current = root / 'current'
    current.symlink_to(target)
    service_file = tmp_path / 'wrapper.conf'
    service_file.write_text('new wrapper definition')
    backup = tmp_path / 'wrapper.backup'
    if prior != 'absent':
        previous.mkdir()
        backup.write_text('old wrapper definition')
    loaded = tmp_path / 'loaded-wrapper'
    loaded.write_text('new group')
    native = tmp_path / 'independent-service'
    native.write_text('unchanged native process')
    calls = tmp_path / 'calls'
    settings = {
        'system': 'linux', 'service_manager': 'supervisor', 'appdir': str(root),
        'target': str(target), 'adopt_root': '', 'current': str(current),
        'previous': str(previous) if prior != 'absent' else '',
        'service_file': str(service_file), 'service_label': 'wrapper',
        'service_had_file': '0' if prior == 'absent' else '1',
        'service_backup': str(backup) if prior != 'absent' else '', 'service_changed': '1',
        'service_stopped': '0', 'service_was_running': '1' if prior == 'running' else '0',
        'stage': '', 'device_backup': '', 'device_changed': '0', 'unit_verify_dir': '',
        'rollback_snapshot': '', 'snapshot_created': '0', 'switched': '1',
        'activation_committed': '0', 'retention_generation': '',
        'test_loaded': str(loaded), 'test_calls': str(calls),
        'test_remove_fails': '1' if remove_fails else '0',
    }
    source = (ROOT / 'deploy/install-wrapper.sh').read_text()
    helpers = source[source.index('restart_after_rollback() {'):]
    helpers = helpers.split('if [ -e "$service_file" ]; then', 1)[0]
    harness = 'set -euo pipefail\n' + '\n'.join(
        f'{key}={shlex.quote(value)}' for key, value in settings.items()
    ) + '\n' + helpers + r'''
linux_service() {
  echo "$1" >> "$test_calls"
  case "$1" in
    restore) cp "$3" "$service_file" ;;
    stop) return 0 ;;
    remove)
      [ ! -e "$service_file" ] || return 1
      [ "$test_remove_fails" -eq 0 ] || return 1
      rm "$test_loaded" ;;
    start|state) return 0 ;;
    *) return 1 ;;
  esac
}
exit 42
'''
    result = subprocess.run(['bash', '-c', harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 42, result.stderr
    operations = calls.read_text().splitlines()
    if prior == 'absent':
        assert not current.exists() and not current.is_symlink()
        assert not service_file.exists()
        assert loaded.exists() == remove_fails
        assert operations == ['stop', 'remove']
        assert ('manual data recovery' in result.stderr) == remove_fails
    else:
        assert current.resolve() == previous
        assert service_file.read_text() == 'old wrapper definition'
        assert loaded.exists()
        assert operations == (['restore', 'start', 'state'] if prior == 'running' else ['restore', 'stop'])
        assert 'code and wrapper data were restored' in result.stderr
    assert native.read_text() == 'unchanged native process'


@pytest.mark.parametrize("previous_install", [False, True])
@pytest.mark.parametrize("phase", ["interrupt-readiness", "success", "fail-output", "retention-failure"])
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
    (target / "deploy/release_retention.py").write_text("raise SystemExit(1)\n")
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
        "system": "darwin", "appdir": str(root), "target": str(target), "adopt_root": "",
        "previous": str(previous) if previous_install else "", "current": str(current),
        "cli_path": str(cli), "target_user": "fixture-user", "target_home": str(tmp_path),
        "service_file": str(tmp_path / "wrapper.plist"), "service_label": "fixture-wrapper",
        "config_dir": str(tmp_path / "config"), "log_dir": str(tmp_path / "logs"),
        "activation_started": "0", "version": "4.0.1", "git_sha": "a" * 40,
        "stage": "", "service_backup": "", "device_backup": "", "unit_verify_dir": "",
        "rollback_snapshot": "", "snapshot_created": "0", "service_changed": "0",
        "device_changed": "0", "service_stopped": "0", "service_was_running": "0",
        "switched": "1", "activation_committed": "0",
        "retention_generation": "fixture-generation" if phase == "retention-failure" else "",
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
        if phase == "retention-failure":
            assert "all backups retained" in result.stdout
