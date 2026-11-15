"""Track deployment workspaces from creation through verified retirement.

Only fresh directories created here become automatic cleanup candidates.
Historical uploads remain subject to the explicit reviewed cleanup inventory.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy import cleanup
from deploy.install_lock import LOCK_FD_ENV, acquire_install_lock, verify_install_lock

REGISTRY = '.deployment-stages.json'
MARKER = '.cc-remote-deployment.json'


def records(root: Path) -> dict:
    path = root / REGISTRY
    if not path.exists():
        return {'schema': 1, 'phase': 'committed', 'stages': []}
    value, _ = cleanup.read_json(path, owner_uid=root.stat().st_uid)
    if (value.get('schema') != 1 or value.get('phase') != 'committed'
            or not isinstance(value.get('stages'), list) or len(value['stages']) > 100):
        raise ValueError('invalid deployment staging registry')
    names = set()
    for row in value['stages']:
        if (not isinstance(row, dict) or not {'path', 'identity', 'phase', 'generation'} <= row.keys()
                or not isinstance(row.get('path'), str)):
            raise ValueError('invalid deployment stage record')
        path = cleanup.absolute_path(row['path'])
        if (path.parent.name != 'deployment-staging' or not re.fullmatch(r'stage-[a-z0-9_]+', path.name)
                or str(path) in names or row.get('phase') not in {'prepared', 'ready'}
                or not isinstance(row.get('identity'), list) or len(row['identity']) != 2
                or not all(type(part) is int and part >= 0 for part in row['identity'])
                or row.get('generation') is not None and (
                    not isinstance(row['generation'], str) or not re.fullmatch(r'[a-f0-9]{32}', row['generation']))):
            raise ValueError('invalid deployment stage identity')
        names.add(str(path))
    return value


def create(root: Path, parent: Path | None = None) -> Path:
    root = cleanup.absolute_path(str(root))
    parent = cleanup.absolute_path(str(parent or root / 'deployment-staging'))
    if parent.name != 'deployment-staging' or parent == root or parent.is_relative_to(root / 'releases'):
        raise ValueError('deployment stages need a dedicated deployment-staging parent')
    data = records(root)
    data['stages'] = [row for row in data['stages'] if Path(row['path']).exists()]
    if len(data['stages']) >= 100:
        raise ValueError('deployment stage limit reached; inspect unfinished deployments')
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='stage-', dir=parent))
    row = {'path': str(stage), 'identity': list(cleanup.identity(stage)),
           'phase': 'prepared', 'generation': None, 'created_at': time.time()}
    cleanup.write_journal(stage / MARKER, {'schema': 1, 'root': str(root), **row})
    data['stages'].append(row)
    cleanup.write_journal(root / REGISTRY, data)
    return stage


def validate(root: Path, row: dict) -> Path:
    stage = cleanup.absolute_path(row['path'])
    if list(cleanup.identity(stage)) != row['identity']:
        raise ValueError('deployment stage was replaced')
    value, _ = cleanup.read_json(stage / MARKER, owner_uid=root.stat().st_uid)
    if value.get('root') != str(root) or value.get('path') != str(stage) or value.get('identity') != row['identity']:
        raise ValueError('deployment stage marker does not match its registration')
    return stage


def bind(root: Path, stage: Path, generation: str) -> None:
    data = records(root)
    row = next((row for row in data['stages'] if row['path'] == str(stage)), None)
    if (row is None or row['phase'] != 'prepared' or row['generation'] not in {None, generation}
            or not re.fullmatch(r'[a-f0-9]{32}', generation)):
        raise ValueError('deployment stage is not available for this activation')
    validate(root, row)
    row['generation'] = generation
    cleanup.write_journal(root / REGISTRY, data)


def check_available(root: Path, stage: Path) -> None:
    row = next((row for row in records(root)['stages'] if row['path'] == str(stage)), None)
    if row is None or row['phase'] != 'prepared' or row['generation'] is not None:
        raise ValueError('deployment stage is not available for activation')
    validate(root, row)


def retire_finished(root: Path, ledger: dict) -> None:
    """Recover a lost finish callback after fresh acceptance under the install lock."""
    data = records(root)
    finished = {row['id'] for row in ledger['generations'] if row['phase'] == 'committed'
                or row['phase'] == 'aborted' and row.get('rollback_verified_at')}
    changed = False
    for row in data['stages']:
        if row['phase'] == 'prepared' and row['generation'] in finished and Path(row['path']).exists():
            validate(root, row)
            row.update(phase='ready', finished_at=time.time())
            changed = True
    if changed:
        cleanup.write_journal(root / REGISTRY, data)


def finish(root: Path, stage: Path) -> None:
    from deploy.release_retention import read_ledger, record_retention
    data = records(root)
    row = next((row for row in data['stages'] if row['path'] == str(stage)), None)
    if row is None:
        raise ValueError('unregistered deployment stage')
    validate(root, row)
    current = str((root / 'current').resolve(strict=True))
    matching = [item for item in read_ledger(root)['generations'] if item['id'] == row['generation']
                and item['release'] == current and item['phase'] == 'committed']
    if len(matching) != 1:
        raise ValueError('deployment stage has no matching committed activation')
    row.update(phase='ready', finished_at=time.time())
    cleanup.write_journal(root / REGISTRY, data)
    record_retention(root, read_ledger(root), status='deferred',
                     reason='completed deployment staging awaits cleanup')


def inventory(root: Path) -> tuple[list[Path], list[Path]]:
    candidates, protected = [], []
    for row in records(root)['stages']:
        if not Path(row['path']).exists():
            continue
        stage = validate(root, row)
        (candidates if row['phase'] == 'ready' else protected).append(stage)
    return candidates, protected


def compact(root: Path) -> None:
    if not (root / REGISTRY).exists():
        return
    data = records(root)
    data['stages'] = [row for row in data['stages'] if Path(row['path']).exists()]
    cleanup.write_journal(root / REGISTRY, data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('create', 'finish'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--parent', type=Path)
    parser.add_argument('--stage', type=Path)
    args = parser.parse_args()
    root = cleanup.absolute_path(str(args.root))
    if args.action == 'create':
        root.mkdir(mode=0o755, parents=True, exist_ok=True)
    inherited = os.environ.get(LOCK_FD_ENV)
    if inherited:
        verify_install_lock(root, int(inherited))
        descriptor = os.dup(int(inherited))
    else:
        descriptor = acquire_install_lock(root)
    try:
        if args.action == 'create':
            print(create(root, args.parent))
        elif args.stage is None:
            parser.error('--stage is required to finish a workspace')
        else:
            finish(root, cleanup.absolute_path(str(args.stage)))
    finally:
        os.close(descriptor)


if __name__ == '__main__':
    main()
