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


def environment_files(path: Path, raw: bytes) -> list[tuple[Path, bool]]:
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
        if b'%p' in value and prefixes:
            for prefix in prefixes:
                result.extend(retention.service_environment_files(key + b'=' + value.replace(b'%p', prefix.encode())))
        else:
            result.extend(retention.service_environment_files(line))
    return result


def snapshot(context: dict, extra_files: list[str], candidates: list[Path] = ()) -> tuple[dict, set[Path], set[Path], dict[Path, bytes]]:
    roots = sorted(map(str, retention.service_roots(context)))
    files = retention.service_files(context)
    transient = {p for p in files if p.is_relative_to(TRANSIENT)}
    retention.require(all(Path(p).is_absolute() and str(Path(p)) == os.path.normpath(p) for p in extra_files),
                      'review inputs must be absolute and normalized')
    # Inputs are observed, never deletion boundaries. Preserve their aliases in
    # the fingerprint instead of either rejecting or prematurely resolving them.
    pending = list(files - transient) + [Path(p) for p in extra_files]
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
        references.update(p for p in candidates if str(p).encode() in raw)
        # Optional absence is also part of the snapshot. Creating that file
        # must invalidate a review rather than silently extending its scope.
        pending.extend(p for p, _ in environment_files(path, raw))
    return {'roots': roots, 'files': records}, transient, references, inputs


def draft(root: Path, context: dict, *, protected_paths: list[str], extra_files: list[str], note: str) -> dict:
    root = cleanup.absolute_path(str(root))
    for path in [*protected_paths, *extra_files]:
        retention.require(Path(path).is_absolute(), 'review paths must be absolute')
    observation, _, _, _ = snapshot(context, extra_files)
    return {'schema': 1, 'status': 'draft', 'installation_root': str(root), 'home': context['home'],
            'protected_paths': protected_paths, 'extra_files': extra_files,
            'note': note, 'snapshot': observation}


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
    value, digest = cleanup.read_json(path)
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
        relative = item.relative_to(TRANSIENT)
        scope = relative.parts[0].endswith(('.scope', '.scope.d'))
        if not scope:
            services.add(item)
            continue
        _, raw, paths = observe(item)
        found.update(str(p) for p in candidates if str(p).encode() in raw)
        found.update(referenced_candidates(paths, candidates))
    found.update(retention.inspect_service_files(services, candidates))
    retention.require(cleanup.read_json(path)[1] == digest, 'service review changed during inspection')
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
