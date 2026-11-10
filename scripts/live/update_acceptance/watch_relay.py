#!/usr/bin/python3
"""Interrupt one real server-side installer inside the disposable Relay only."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

root = Path('/opt/cc-remote')
mode = sys.argv[1]
deadline = time.monotonic() + 220
while time.monotonic() < deadline:
    try:
        value = json.loads((root / 'activation.json').read_text())
        manifest = json.loads((root / 'current/release-manifest.json').read_text())
    except (OSError, ValueError):
        value, manifest = {}, {}
    if value.get('phase') != 'stopping' or manifest.get('product_version') != '5.3.19':
        time.sleep(.02)
        continue
    unit = 'cc-remote-update-' + value['id']
    pid = int(subprocess.check_output(['systemctl', 'show', unit, '--property=MainPID', '--value'], text=True).strip())
    if pid <= 0:
        raise RuntimeError('original server job has no main process')
    if mode == 'relay-installer-kill':
        candidates = []
        for folder in Path('/proc').iterdir():
            if not folder.name.isdecimal():
                continue
            try:
                args = (folder / 'cmdline').read_bytes().split(b'\0')
                if not any(arg.endswith(b'/deploy/setup-vps.sh') and b'/cc-remote-update-' in arg for arg in args):
                    continue
                parent = int(folder.name)
                ancestors = set()
                while parent > 1 and parent not in ancestors:
                    ancestors.add(parent)
                    parent = int(Path(f'/proc/{parent}/stat').read_text().rsplit(')', 1)[1].split()[1])
                if pid in ancestors:
                    candidates.append(int(folder.name))
            except (OSError, ValueError, IndexError):
                continue
        if len(candidates) != 1:
            raise RuntimeError(f'could not identify original server installer: {candidates}')
        pid = candidates[0]
    (root / 'acceptance-watch.json').write_text(json.dumps({'mode': mode, 'pid': pid, 'id': value['id'], 'unit': unit, 'at': time.time()}))
    os.kill(pid, signal.SIGKILL)
    raise SystemExit(0)
raise TimeoutError('server switch was not reached')
