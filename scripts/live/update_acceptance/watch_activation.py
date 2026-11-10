#!/usr/bin/python3
"""Fault injector for the disposable Wrapper container, scoped to one request."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time

identity, mode = sys.argv[1:]
root = Path('/opt/cc-remote-wrapper')
deadline = time.monotonic() + 220
while time.monotonic() < deadline:
    try:
        value = json.loads((root / 'activation.json').read_text())
    except (OSError, ValueError):
        value = {}
    if value.get('id') != identity or value.get('phase') != 'verifying':
        time.sleep(.02)
        continue
    state = json.loads((root / 'ui-update-jobs' / (identity + '.json')).read_text())
    pid = state['worker_pid']
    if mode == 'worker-kill':
        args = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        if identity.encode() not in args or b'run' not in args:
            raise RuntimeError('controller identity changed')
    elif mode == 'installer-kill':
        candidates = []
        for folder in Path('/proc').iterdir():
            if not folder.name.isdecimal():
                continue
            try:
                args = (folder / 'cmdline').read_bytes().split(b'\0')
                if not any(a.endswith(b'/deploy/install-wrapper.sh') and b'/cc-remote-update-' in a for a in args):
                    continue
                parent = int((folder / 'stat').read_text().rsplit(')', 1)[1].split()[1])
                if parent == pid:
                    candidates.append(int(folder.name))
            except (OSError, ValueError, IndexError):
                continue
        if len(candidates) != 1:
            raise RuntimeError(f'could not identify the unique original installer: {candidates}')
        pid = candidates[0]
    if mode != 'worker-kill':
        database = Path('/home/tester/.claude/cc-remote/work/registry.sqlite3')
        with sqlite3.connect(database) as connection:
            connection.execute("update acceptance_canary set value='provisional'")
    evidence = {'id': identity, 'mode': mode, 'pid': pid, 'phase': value['phase'], 'at': time.time()}
    (root / 'acceptance-watch.json').write_text(json.dumps(evidence))
    if mode in {'worker-kill', 'installer-kill'}:
        os.kill(pid, signal.SIGKILL)
    raise SystemExit(0)
raise TimeoutError('target activation phase was not reached')
