#!/usr/bin/env python3
"""One bounded foreground command for an explicitly authorized live model probe."""
import argparse
import json
import os
from pathlib import Path
import time


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('engine', choices=['claude', 'codex', 'dsh', 'claude-queue'])
    parser.add_argument('--seconds', type=int, default=300)
    args = parser.parse_args()
    folder = Path(__file__).resolve().parent
    # Exclusive creation detects an accidentally duplicated model command.
    with (folder / (args.engine + '-heartbeat.jsonl')).open('x') as output:
        deadline = time.monotonic() + min(max(args.seconds, 1), 600)
        while time.monotonic() < deadline:
            output.write(json.dumps({'at': time.time(), 'pid': os.getpid(), 'ppid': os.getppid()}) + '\n')
            output.flush()
            if (folder / ('finish-' + args.engine)).exists():
                print('CCR_UPDATE_' + args.engine.upper() + '_OK', flush=True)
                break
            time.sleep(1)
        else:
            raise TimeoutError('acceptance controller did not release the probe')
