"""Authorization boundaries of the root broker; live socket activation is separately accepted."""
import pytest
from deploy import ui_update_broker as broker, ui_update_job
from tests.test_ui_updates import managed as managed


def test_broker_accepts_only_owning_uid_and_closed_operation_schema(managed, monkeypatch):
    root, _, store = managed
    value, account = ui_update_job.installation(root)
    monkeypatch.setattr(ui_update_job, 'installation', lambda _: (value, account))
    calls = []
    monkeypatch.setattr(ui_update_job, 'launch', lambda *a: calls.append(a) or {'id': a[1]})
    command = {'op': 'start', 'id': 'd' * 32, 'version': '4.0.1'}
    assert broker.dispatch(root, command, account.pw_uid)['ok']
    assert len(calls) == 1
    for uid, request in [(account.pw_uid + 1, command),
                         (account.pw_uid, {**command, 'command': 'anything'}),
                         (account.pw_uid, {**command, 'root': '/another/device'}),
                         (account.pw_uid, {'op': 'shell', 'id': 'd' * 32})]:
        with pytest.raises(ValueError):
            broker.dispatch(root, request, uid)
    assert len(calls) == 1
    assert store.policy()['enabled']


def test_broker_unit_arguments_never_expand_environment_or_specifiers():
    assert broker.unit_argument('/opt/an installation/$x%u"\\') == '"/opt/an installation/$$x%%u\\"\\\\"'
    assert broker.unit_argument('/opt/$x%u', expand_environment=False) == '"/opt/$x%%u"'
    with pytest.raises(ValueError):
        broker.unit_argument('/opt/bad\nExecStart=other')


def test_cancel_before_os_worker_claim_fences_delayed_start(managed, monkeypatch):
    root, _, store = managed
    identity = 'f' * 32
    store.create(identity, '4.0.1', '4.0.0')
    monkeypatch.setattr(ui_update_job, 'update', lambda **kw: pytest.fail('cancelled worker installed'))
    retired = []
    monkeypatch.setattr(ui_update_job, 'retire', lambda *args: retired.append(args))
    assert ui_update_job.cancel(root, identity)['phase'] == 'cancelled'
    ui_update_job.run(root, identity)
    assert store.state(identity)['phase'] == 'cancelled'
    assert retired == [(root, identity)]
