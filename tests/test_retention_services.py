"""Explicit service reviews are invalidated, never silently broadened."""
from __future__ import annotations

import json
from pathlib import Path
import plistlib

import pytest

from deploy import release_retention as retention, retention_services as reviews


def test_user_home_specifier_only_resolves_in_that_users_own_units(tmp_path):
    home = tmp_path.resolve()
    unit = home / '.config/systemd/user/worker.service'
    raw = b'[Service]\nEnvironmentFile=%h/.config/worker.env\n'
    assert reviews.environment_files(unit, raw, home=home) == [(home / '.config/worker.env', False)]
    with pytest.raises(ValueError):
        reviews.environment_files(Path('/usr/lib/systemd/user/worker.service'), raw, home=home)


@pytest.mark.parametrize('path', ['/run/systemd/generator.late/a.service',
                                 '/run/user/1000/systemd/transient/a.service',
                                 '/run/user/1000/systemd/generator.late/a.service'])
def test_runtime_unit_paths_are_always_reinspected(path):
    assert reviews.runtime_unit_root(Path(path)) is not None
    assert reviews.runtime_unit_root(Path('/etc/systemd/system/a.service')) is None


def test_plist_argument_with_spaces_retains_intermediate_alias_owner(tmp_path):
    old = tmp_path.resolve() / 'old'
    old.mkdir()
    (old / 'payload').write_text('runtime')
    alias = tmp_path.resolve() / 'resource with spaces'
    alias.symlink_to(old / 'payload')
    service = tmp_path.resolve() / 'worker.plist'
    service.write_bytes(plistlib.dumps({'ProgramArguments': ['/usr/bin/cat', str(alias)]}))
    assert retention.inspect_service_files({service}, [old]) == [str(old)]


def test_dependency_matching_respects_components_and_nested_candidates():
    candidates = list(map(Path, ['/srv/build', '/srv/build/runtime', '/srv/build-copy', '/srv/other']))
    references = list(map(Path, ['/srv/build/runtime/lib/module.py', '/srv/build-copycat/file']))
    assert reviews.referenced_candidates(references, candidates) == {'/srv/build', '/srv/build/runtime'}


@pytest.fixture
def site(tmp_path, monkeypatch):
    root = tmp_path / 'install'
    old = root / 'releases/old'
    old.mkdir(parents=True)
    (old / 'payload').write_text('runtime')
    units = tmp_path / 'units'
    transient = tmp_path / 'run/transient'
    units.mkdir()
    transient.mkdir(parents=True)
    service = units / 'legacy.service'
    service.write_text('[Service]\nExecStart=/bin/sh -c true\n')
    context = {'home': str(tmp_path)}
    monkeypatch.setattr(retention, 'service_roots', lambda row: [units, transient])
    monkeypatch.setattr(reviews, 'TRANSIENT', transient)

    def approve(protected=(), extras=()):
        value = reviews.draft(root, context, protected_paths=list(map(str, protected)),
                              extra_files=list(map(str, extras)), note='Inspected this legacy command and its inputs.')
        value['status'] = 'reviewed'
        path = root / reviews.REVIEW
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return {**context, 'review': str(path)}

    return root, old, units, transient, service, context, approve


def test_review_is_opt_in_and_does_not_silence_default_guard(site):
    _, old, _, _, _, context, approve = site
    with pytest.raises(ValueError, match='indirect service command'):
        retention.service_dependencies(context, [old])
    assert retention.service_dependencies(approve(), [old]) == []


def test_reviewed_runtime_command_keeps_dependencies_and_detects_changed_input(site):
    _, old, _, transient, _, _, approve = site
    service = transient / 'legacy.service'
    service.write_text(f'[Service]\nExecStart=/bin/sh -c "cat {old}/payload"\n')
    context = approve()
    assert retention.service_dependencies(context, [old]) == [str(old)]
    service.write_text('[Service]\nExecStart=/bin/sh -c "$CHANGED"\n')
    with pytest.raises(ValueError, match='indirect service command'):
        retention.service_dependencies(context, [old])
    service.unlink()
    assert retention.service_dependencies(context, [old]) == []


def test_review_keeps_direct_references_even_when_candidate_was_not_declared(site):
    _, old, _, _, service, _, approve = site
    service.write_text(f'[Service]\nExecStart=/bin/sh -c "cat {old}/payload"\n')
    assert retention.service_dependencies(approve(), [old]) == [str(old)]


@pytest.mark.parametrize('setting', [
    'ExecStart={alias}',
    'ExecStart=/usr/bin/python3 -I {alias}',
    'ExecStart={alias} --mode=$MODE',
    'ExecStart=/bin/sh -c "$COMMAND" {alias}',
    'WorkingDirectory={alias}',
    'Environment=CONFIG={alias}',
])
def test_review_preserves_literal_dependency_aliases_without_explicit_protect(site, setting):
    _, old, units, _, service, _, approve = site
    alias = units.parent / 'external-worker'
    alias.symlink_to(old / 'payload')
    service.write_text('[Service]\n' + setting.format(alias=alias) + '\n')
    assert retention.service_dependencies(approve(), [old]) == [str(old)]


def test_review_rescans_literal_aliases_including_intermediate_release_owners(site):
    _, old, units, _, service, _, approve = site
    outside = units.parent / 'other-worker'
    outside.write_text('outside runtime')
    alias = units.parent / 'external-worker'
    alias.symlink_to(outside)
    service.write_text(f'[Service]\nExecStart={alias}\n')
    context = approve()
    assert retention.service_dependencies(context, [old]) == []
    (old / 'indirect-worker').symlink_to(outside)
    alias.unlink()
    alias.symlink_to(old / 'indirect-worker')
    assert retention.service_dependencies(context, [old]) == [str(old)]
    outside.unlink()
    with pytest.raises(OSError):
        retention.service_dependencies(context, [old])


def test_review_preserves_environment_and_bare_executable_search_dependencies(site, monkeypatch):
    _, old, units, _, service, _, approve = site
    search = units.parent / 'bin'
    search.mkdir()
    (search / 'worker').symlink_to(old / 'payload')
    (old / 'payload').chmod(0o755)
    env = units.parent / 'legacy.env'
    env.write_text(f'PATH={search}:$OTHER_PATH\n')
    service.write_text(f'[Service]\nExecStart=worker\nEnvironmentFile={env}\n')
    monkeypatch.setattr(retention, 'systemd_executable_paths', lambda: [])
    assert retention.service_dependencies(approve(), [old]) == [str(old)]


def test_review_preserves_plist_program_and_argument_aliases(site):
    _, old, units, _, service, _, approve = site
    service.unlink()
    alias = units.parent / 'worker-script'
    alias.symlink_to(old / 'payload')
    (units / 'worker.plist').write_bytes(plistlib.dumps({
        'Label': 'worker', 'Program': '/usr/bin/python3',
        'ProgramArguments': ['python3', '-I', str(alias)],
    }))
    assert retention.service_dependencies(approve(), [old]) == [str(old)]


def test_review_tracks_prefix_environment_file_and_alias_superset(site):
    _, old, units, _, service, _, approve = site
    service.write_text(f'[Service]\nEnvironmentFile=-{units.parent}/%p.env\n')
    alias = units / 'other.service'
    alias.symlink_to(service)
    env = units.parent / 'legacy.env'
    env.write_text('VALUE=original\n')
    context = approve()
    assert retention.service_dependencies(context, [old]) == []
    (units.parent / 'other.env').write_text('VALUE=added\n')
    with pytest.raises(ValueError, match='configuration changed'):
        retention.service_dependencies(context, [old])


@pytest.mark.parametrize('value', ['%i', '%p/%i', '%%p', '$UNKNOWN'])
def test_review_does_not_guess_dynamic_environment_inputs(site, value):
    _, _, _, _, service, _, approve = site
    service.write_text(f'[Service]\nEnvironmentFile=-/etc/{value}.env\n')
    with pytest.raises(ValueError, match='dynamic or ambiguous'):
        approve()


@pytest.mark.parametrize('change', ['unit', 'new-file', 'environment', 'optional-created', 'extra', 'roots'])
def test_any_changed_review_input_defers_retirement(site, monkeypatch, change):
    _, old, units, _, service, _, approve = site
    env = units.parent / 'service.env'
    extra = units.parent / 'legacy-script'
    extra.write_text('known script input')
    if change != 'optional-created':
        env.write_text('KEY=one\n')
    service.write_text(f'[Service]\nExecStart=/bin/sh -c true\nEnvironmentFile=-{env}\n')
    context = approve(extras=[extra])
    if change == 'unit':
        service.write_text('[Service]\nExecStart=/bin/sh -c changed\n')
    elif change == 'new-file':
        (units / 'new.service').write_text('[Service]\nExecStart=/bin/true\n')
    elif change in {'environment', 'optional-created'}:
        env.write_text('KEY=two\n')
    elif change == 'extra':
        extra.write_text('changed dependency')
    else:
        original = retention.service_roots
        monkeypatch.setattr(retention, 'service_roots', lambda row: [*original(row), units / 'new-load-path'])
    with pytest.raises(ValueError, match='configuration changed'):
        retention.service_dependencies(context, [old])


def test_reviewed_aliases_are_resolved_again_and_keep_their_runtime(site):
    _, old, units, _, _, _, approve = site
    alias = units.parent / 'worker-runtime'
    other = units.parent / 'other'
    other.mkdir()
    alias.symlink_to(other)
    context = approve(protected=[alias])
    assert retention.service_dependencies(context, [old]) == []
    alias.unlink()
    alias.symlink_to(old)
    assert retention.service_dependencies(context, [old]) == [str(old)]


def test_indirect_script_input_alias_is_fingerprinted(site):
    _, old, units, _, _, _, approve = site
    original = units.parent / 'original-script'
    replacement = units.parent / 'replacement-script'
    original.write_text('same content')
    replacement.write_text('same content')
    alias = units.parent / 'script-alias'
    alias.symlink_to(original)
    context = approve(extras=[alias])
    assert retention.service_dependencies(context, [old]) == []
    alias.unlink()
    alias.symlink_to(replacement)
    with pytest.raises(ValueError, match='configuration changed'):
        retention.service_dependencies(context, [old])


def test_review_cannot_hide_changed_service_alias_or_dangling_target(site):
    _, old, units, _, _, _, approve = site
    target = units.parent / 'missing.service'
    (units / 'alias.service').symlink_to(target)
    context = approve()
    assert retention.service_dependencies(context, [old]) == []
    target.write_text(f'[Service]\nExecStart={old}/payload\n')
    with pytest.raises(ValueError, match='configuration changed'):
        retention.service_dependencies(context, [old])


def test_transient_units_are_inspected_even_after_a_review(site):
    _, old, _, transient, _, _, approve = site
    context = approve()
    service = transient / 'new.service'
    service.write_text(f'[Service]\nExecStart={old}/payload\n')
    assert retention.service_dependencies(context, [old]) == [str(old)]
    service.write_text('[Service]\nExecStart=/usr/bin/python3 -m hidden_worker\n')
    with pytest.raises(ValueError, match='indirect service command'):
        retention.service_dependencies(context, [old])


def test_login_scope_churn_does_not_invalidate_review_but_keeps_references(site):
    _, old, _, transient, _, _, approve = site
    context = approve()
    scope = transient / 'session-1.scope'
    scope.write_text('[Scope]\nDescription=Login session\n')
    assert retention.service_dependencies(context, [old]) == []
    scope.write_text(f'[Scope]\nDescription=Process in {old}\n')
    assert retention.service_dependencies(context, [old]) == [str(old)]


@pytest.mark.parametrize('invalid', ['draft', 'mode', 'home', 'root', 'symlink', 'unreadable'])
def test_invalid_or_unreadable_review_never_allows_cleanup(site, monkeypatch, invalid):
    root, old, _, _, service, _, approve = site
    context = approve()
    path = root / reviews.REVIEW
    value = json.loads(path.read_text())
    if invalid == 'draft':
        value['status'] = 'draft'
    elif invalid == 'mode':
        path.chmod(0o644)
    elif invalid == 'home':
        value['home'] = '/different'
    elif invalid == 'root':
        value['installation_root'] = '/different'
    elif invalid == 'symlink':
        other = path.with_suffix('.other')
        path.rename(other)
        path.symlink_to(other)
    elif invalid == 'unreadable':
        original = Path.read_bytes

        def denied(p):
            if p == service:
                raise PermissionError('cannot inspect current service')
            return original(p)

        monkeypatch.setattr(Path, 'read_bytes', denied)
    if invalid in {'draft', 'home', 'root'}:
        path.write_text(json.dumps(value))
    with pytest.raises((ValueError, OSError, retention.retirement.CleanupError)):
        retention.service_dependencies(context, [old])
