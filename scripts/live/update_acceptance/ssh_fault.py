#!/usr/bin/python3
"""ForceCommand for a disposable acceptance account; never install on a real account."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

folder = Path.home() / 'ccr-acceptance'
control = folder / 'ssh.json'
command = os.environ['SSH_ORIGINAL_COMMAND']
args = shlex.split(command)
kind, unit = 'other', None
try:
    request = json.loads(args[-1])
    if isinstance(request, dict) and str(request.get('unit', '')).startswith('cc-remote-update-'):
        kind = 'inspect' if request.get('operation') == 'inspect' else 'submit'
        unit = request['unit']
except (ValueError, IndexError):
    if 'systemctl' in args and 'show' in args and any(a.startswith('cc-remote-update-') for a in args):
        kind = 'status'
    elif '-c' in args:
        kind = 'verify'
state = json.loads(control.read_text()) if control.exists() else {}
state.setdefault('calls', []).append({'at': time.time(), 'kind': kind, 'unit': unit})
fault = state.get('kind') == kind and state.get('remaining', 0) > 0
if fault:
    state['remaining'] -= 1
folder.mkdir(mode=0o700, exist_ok=True)
control.write_text(json.dumps(state))
if fault and state.get('mode') != 'lost-ack':
    raise SystemExit(255)
result = subprocess.run(command, shell=True, capture_output=fault, check=False)
raise SystemExit(255 if fault else result.returncode)
