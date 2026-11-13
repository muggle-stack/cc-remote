"""Reusable operator-reviewed service inventory for indirect service commands.

The default scanner stays strict. This opt-in binds a private review to the
complete persistent service/configuration inventory. A changed file, alias,
search path or environment file invalidates it. Transient services are always
inspected afresh; process/open-file and dependency closure checks still run.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import cleanup, release_retention as retention

REVIEW = '.retention-services.json'
TRANSIENT = Path('/run/systemd/transient')


def runtime_unit_root(path: Path) -> Path | None:
    """Generated/user-runtime units are re-inspected, never statically approved."""
    roots = [TRANSIENT, Path('/run/systemd'), *[Path('/run/user') / part / 'systemd'
                                  for part in path.parts[3:4] if part.isdecimal()]]
    for root in roots:
        if path.is_relative_to(root):
            return root
    return None


def observe(path: Path) -> tuple[dict, bytes, list[Path]]:
    """Bind intermediate aliases too, including a reviewed dangling unit link."""
    pending = list(path.parts[1:])
    current = Path('/')
    links, references = [], []
    while pending:
        part = pending.pop(0)
        if part == '..':
            current = current.parent
            continue
        current /= part
        references.append(current)
        try:
            info = current.lstat()
        except FileNotFoundError:
            current = current.joinpath(*pending)
            break
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(current)
            links.append([str(current), target])
            retention.require(len(links) <= 64, 'cyclic service alias')
            next_path = Path(target)
            current = Path('/') if next_path.is_absolute() else current.parent
            pending = list(next_path.parts[1:] if next_path.is_absolute() else next_path.parts) + pending
    value = {'resolved': str(current), 'links': links}
    try:
        info = current.stat()
    except FileNotFoundError:
        return {**value, 'kind': 'missing'}, b'', references
    if current == Path('/dev/null') and stat.S_ISCHR(info.st_mode):
        return {**value, 'kind': 'masked'}, b'', references
    retention.require(stat.S_ISREG(info.st_mode) and info.st_size <= 4 * 1024 * 1024,
                      f'service inventory file is not a bounded regular file: {path}')
    raw = current.read_bytes()
    return {**value, 'kind': 'file', 'sha256': hashlib.sha256(raw).hexdigest(),
            'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid}, raw, references


def environment_files(path: Path, raw: bytes, *, home: Path | None = None) -> list[tuple[Path, bool]]:
    """Resolve only the deterministic %p of a literal, non-template unit.

    Other specifiers remain fail-closed. Inspect both alias and canonical names
    as a superset, rather than guessing which alias systemd will activate.
    """
    names = {path.name, path.resolve().name}
    if path.parent.name.endswith('.service.d'):
        names.add(path.parent.name.removesuffix('.d'))
    prefixes = {name.removesuffix('.service') for name in names
                if re.fullmatch(r'[A-Za-z0-9_.:-]+\.service', name)}
    result = []
    for line in retention.service_lines(raw):
        key, separator, value = line.partition(b'=')
        if not separator or key.strip() != b'EnvironmentFile':
            continue
        if home is not None and path.is_relative_to(home / '.config/systemd/user'):
            value = value.replace(b'%h', os.fsencode(home))
        if b'%p' in value and prefixes:
            for prefix in prefixes:
                result.extend(retention.service_environment_files(key + b'=' + value.replace(b'%p', prefix.encode())))
        else:
            result.extend(retention.service_environment_files(key + b'=' + value))
    return result


def observe_inputs(paths: list[Path], home: Path, candidates: list[Path]) -> tuple[dict, set[Path], dict[Path, bytes]]:
    pending = list(paths)
    markers = [(p, os.fsencode(p)) for p in candidates]
    records, references, inputs = {}, set(), {}
    size = 0
    while pending:
        path = pending.pop()
        if str(path) in records:
            continue
        value, raw, paths = observe(path)
        size += len(raw)
        retention.require(len(records) < 8192 and size <= 32 * 1024 * 1024,
                          'service inventory exceeds limit')
        records[str(path)] = value
        inputs[path] = raw
        references.update(paths)
        # A review must never erase direct references, including candidates that
        # only become eligible in a later deployment generation.
        references.update(p for p, marker in markers if marker in raw)
        # Optional absence is also part of the snapshot. Creating that file
        # must invalidate a review rather than silently extending its scope.
        pending.extend(p for p, _ in environment_files(path, raw, home=home))
    return records, references, inputs


def snapshot(context: dict, extra_files: list[str], candidates: list[Path] = ()) -> tuple[dict, set[Path], set[Path], dict[Path, bytes]]:
    roots = sorted(str(p) for p in retention.service_roots(context) if runtime_unit_root(p) is None)
    files = retention.service_files(context)
    transient = {p for p in files if runtime_unit_root(p) is not None}
    retention.require(all(Path(p).is_absolute() and str(Path(p)) == os.path.normpath(p) for p in extra_files),
                      'review inputs must be absolute and normalized')
    records, references, inputs = observe_inputs(
        [*files - transient, *map(Path, extra_files)], Path(context['home']), candidates)
    return {'roots': roots, 'files': records}, transient, references, inputs


def draft(root: Path, context: dict, *, protected_paths: list[str], extra_files: list[str], note: str) -> dict:
    root = cleanup.absolute_path(str(root))
    for path in [*protected_paths, *extra_files]:
        retention.require(Path(path).is_absolute(), 'review paths must be absolute')
    observation, transient, _, _ = snapshot(context, extra_files)
    runtime, _, _ = observe_inputs(list(transient), Path(context['home']), [])
    return {'schema': 1, 'status': 'draft', 'installation_root': str(root), 'home': context['home'],
            'protected_paths': protected_paths, 'extra_files': extra_files,
            'note': note, 'snapshot': observation, 'runtime_inputs': runtime}


def referenced_candidates(references: set[Path] | list[Path], candidates: list[Path]) -> set[str]:
    """Match complete path components without a candidate × reference scan."""
    names = {str(p) for p in candidates}
    found = set()
    for reference in references:
        name = str(reference)
        while name:
            if name in names:
                found.add(name)
            if name == '/':
                break
            name = name.rpartition('/')[0] or '/'
    return found


def reviewed_dependencies(context: dict, candidates: list[Path]) -> list[str]:
    path = cleanup.absolute_path(context['review'])
    root = path.parent
    info = path.stat()
    retention.require(path.name == REVIEW and info.st_uid == root.stat().st_uid
                      and not stat.S_IMODE(info.st_mode) & 0o077,
                      'service review must be private and owned by the installation owner')
    value, digest = cleanup.read_json(path, owner_uid=root.stat().st_uid)
    retention.require(value.get('schema') == 1 and value.get('status') == 'reviewed'
                      and value.get('installation_root') == str(root) and value.get('home') == context['home']
                      and isinstance(value.get('note'), str) and bool(value['note'].strip()),
                      'service review is incomplete or belongs to another installation')
    for key in ('extra_files', 'protected_paths'):
        retention.require(isinstance(value.get(key), list) and len(value[key]) <= 128
                          and all(isinstance(p, str) and Path(p).is_absolute() for p in value[key]),
                          'invalid reviewed dependency paths')
    observation, transient, references, inputs = snapshot(context, value['extra_files'], candidates)
    retention.require(observation == value.get('snapshot'),
                      'service configuration changed; inspect and renew the service review')
    found = referenced_candidates(references, candidates)
    found.update(retention.inspect_service_files(set(inputs), candidates, reviewed_inputs=inputs))
    for dependency in value['protected_paths']:
        found.update(retention.service_path_dependencies(Path(dependency), candidates))
    # A scope groups already-running processes and has no dormant executable.
    # Their real cwd/open files are inspected separately by cleanup.py. Still
    # preserve any candidate named in scope metadata or a drop-in alias.
    services = set()
    for item in transient:
        relative = item.relative_to(runtime_unit_root(item))
        scope = any(part.endswith(('.scope', '.scope.d')) for part in relative.parts)
        if not scope:
            records, references, inputs = observe_inputs([item], Path(context['home']), candidates)
            found.update(referenced_candidates(references, candidates))
            approved = value.get('runtime_inputs', {})
            if all(record == approved.get(name) for name, record in records.items()):
                found.update(retention.inspect_service_files(set(inputs), candidates, reviewed_inputs=inputs))
            else:
                services.add(item)
            continue
        _, raw, paths = observe(item)
        found.update(str(p) for p in candidates if str(p).encode() in raw)
        found.update(referenced_candidates(paths, candidates))
    found.update(retention.inspect_service_files(services, candidates))
    retention.require(cleanup.read_json(path, owner_uid=root.stat().st_uid)[1] == digest,
                      'service review changed during inspection')
    return sorted(found)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--protect', action='append', default=[])
    parser.add_argument('--extra-file', action='append', default=[])
    parser.add_argument('--note', required=True)
    args = parser.parse_args()
    root = cleanup.absolute_path(str(args.root))
    rows = retention.read_ledger(root)['generations']
    row = next(r for r in rows if r['release'] == str((root / 'current').resolve()) and r['phase'] == 'committed')
    retention.require(not args.output.exists(), 'review output already exists')
    value = draft(root, row, protected_paths=args.protect, extra_files=args.extra_file, note=args.note)
    cleanup.write_journal(args.output, value)
    print('Draft saved. Inspect indirect commands, their external inputs and protected paths before marking it reviewed.')


if __name__ == '__main__':
    main()
