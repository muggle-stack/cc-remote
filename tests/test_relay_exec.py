"""The service always imports its own immutable Relay release."""
import os
from pathlib import Path
import subprocess
import sys


def test_relay_entrypoint_ignores_ambient_cwd_and_pythonpath(tmp_path):
    release = tmp_path / 'release with spaces'
    deploy = release / 'deploy'
    deploy.mkdir(parents=True)
    script = deploy / 'relay_exec.py'
    script.write_bytes((Path(__file__).resolve().parents[1] / 'deploy/relay_exec.py').read_bytes())
    for root, marker in [(release, 'correct-release'), (tmp_path / 'ambient', 'wrong-release')]:
        package = root / 'cc_remote/relay'
        package.mkdir(parents=True)
        (root / 'cc_remote/__init__.py').touch()
        (package / '__init__.py').touch()
        (package / '__main__.py').write_text(f'print({marker!r})\n')
    link = tmp_path / 'current'
    link.symlink_to(release)
    result = subprocess.run([sys.executable, '-I', '-B', str(link / 'deploy/relay_exec.py')],
                            cwd=tmp_path / 'ambient', env={**os.environ, 'PYTHONPATH': str(tmp_path / 'ambient')},
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'correct-release'
