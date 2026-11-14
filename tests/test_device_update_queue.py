"""Persistent offline update requests keep device ownership and exact ids."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from cc_remote.relay.devices import DeviceStore
from cc_remote.relay.updates import Peer, UpdateChannelError, UpdateRelay
from cc_remote.relay import server
from tests.test_devices import _cfg


ID = 'a' * 32
STATUS = {'schema': 1, 'managed': True, 'enabled': True, 'current': '5.3.26',
          'latest': {'version': '5.3.28', 'notes': ''}, 'busy_count': 0,
          'allow_relay_update': False, 'reason': None}
START = {'op': 'start', 'job_id': ID, 'version': '5.3.28'}


@pytest.fixture
def hub(tmp_path):
    result = UpdateRelay(DeviceStore(str(tmp_path / 'devices.sqlite3')))
    result.queue.remember('device-a', STATUS)
    return result


@pytest.mark.asyncio
async def test_offline_request_survives_relay_restart_and_is_device_scoped(hub):
    result = await hub.request('device-a', START)
    assert result['ok'] and result['job']['phase'] == 'waiting_device'
    restored = UpdateRelay(DeviceStore(str(hub.queue.devices.path)))
    status = await restored.request('device-a', {'op': 'status'})
    assert status['online'] is False and status['maintenance'] is False
    assert status['job']['id'] == ID
    lookup = await restored.request('device-a', {'op': 'lookup', 'job_id': ID})
    assert lookup['receipt'] == 'found' and lookup['job']['version'] == '5.3.28'
    assert (await restored.request('device-b', {'op': 'lookup', 'job_id': ID}))['receipt'] == 'missing'
    assert await restored.request('device-a', START) == result
    assert (await restored.request('device-a', {**START, 'job_id': 'b' * 32}))['reason'] == 'busy'


@pytest.mark.asyncio
async def test_offline_cancel_fences_delivery_before_device_returns(hub):
    await hub.request('device-a', START)
    result = await hub.request('device-a', {'op': 'cancel', 'job_id': ID})
    assert result['job']['phase'] == 'cancelled'
    peer = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    hub.peers['device-a'] = peer
    hub._request = AsyncMock(side_effect=AssertionError('cancelled intent must not be delivered'))
    await hub._deliver('device-a', peer)
    hub._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconnect_delivers_original_request_and_records_device_receipt(hub):
    submitted = await hub.request('device-a', START)
    peer = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    hub.peers['device-a'] = peer
    hub._request = AsyncMock(return_value={'ok': True, 'job': {**submitted['job'], 'phase': 'submitting'}})
    await hub._deliver('device-a', peer)
    hub._request.assert_awaited_once_with('device-a', START)
    record = hub.queue.job('device-a', ID)
    assert record['delivered'] and record['phase'] == 'submitting'


@pytest.mark.asyncio
async def test_lost_reply_keeps_same_request_until_reconnect(hub):
    peer = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    hub.peers['device-a'] = peer
    calls = []
    async def respond(machine, command):
        calls.append(command)
        if command['op'] == 'status':
            return STATUS
        raise UpdateChannelError('outcome_pending')
    hub._request = respond
    result = await hub.request('device-a', START)
    assert result['job']['phase'] == 'unknown'
    state = hub.queue.job('device-a', ID)
    hub._request = AsyncMock(return_value={'ok': True, 'job': {**state, 'phase': 'downloading'}})
    await hub._deliver('device-a', peer)
    assert calls[-1] == START
    hub._request.assert_awaited_once_with('device-a', START)
    assert hub.queue.job('device-a', ID)['phase'] == 'downloading'


@pytest.mark.asyncio
async def test_live_peer_missing_start_is_reconciled_with_same_identity(hub):
    await hub.request('device-a', START)
    state = hub.queue.job('device-a', ID)
    hub.peers['device-a'] = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    hub._request = AsyncMock(side_effect=[{**STATUS, 'job': None},
                                         {'ok': True, 'job': {**state, 'phase': 'downloading'}}])
    result = await hub.request('device-a', {'op': 'status'})
    assert result['job']['phase'] == 'downloading'
    assert hub._request.await_args_list[1].args == ('device-a', START)


@pytest.mark.asyncio
async def test_disabled_device_policy_rejects_a_queued_request_on_return(hub):
    await hub.request('device-a', START)
    peer = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    hub.peers['device-a'] = peer
    hub._request = AsyncMock(return_value={'ok': False, 'reason': 'setup_required'})
    await hub._deliver('device-a', peer)
    state = hub.queue.job('device-a', ID)
    assert state['phase'] == 'blocked' and state['reason'] == 'setup_required'


@pytest.mark.asyncio
async def test_offline_requests_require_a_previously_verified_update_policy(hub):
    result = await hub.request('device-b', START)
    assert result == {'ok': False, 'reason': 'setup_required'}
    assert hub.queue.active('device-b') is None


@pytest.mark.asyncio
async def test_an_offline_cancellation_after_delivery_is_forwarded_on_return(hub):
    await hub.request('device-a', START)
    state = hub.queue.job('device-a', ID)
    hub.queue.save('device-a', {**state, 'delivered': True})
    cancelled = await hub.request('device-a', {'op': 'cancel', 'job_id': ID})
    assert cancelled['job']['cancel_requested'] and cancelled['job']['phase'] == 'waiting_device'
    peer = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    hub.peers['device-a'] = peer
    hub._request = AsyncMock(return_value={'ok': True, 'job': {**state, 'phase': 'cancelled'}})
    await hub._deliver('device-a', peer)
    hub._request.assert_awaited_once_with('device-a', {'op': 'cancel', 'job_id': ID})


@pytest.mark.parametrize('reported', [False, None, 'omitted'])
@pytest.mark.asyncio
async def test_status_observation_preserves_unacknowledged_cancellation(hub, reported):
    submitted = await hub.request('device-a', START)
    hub.queue.save('device-a', {**hub.queue.job('device-a', ID), 'delivered': True})
    await hub.request('device-a', {'op': 'cancel', 'job_id': ID})
    observed = {**submitted['job'], 'phase': 'downloading'}
    if reported == 'omitted':
        observed.pop('cancel_requested', None)
    else:
        observed['cancel_requested'] = reported
    hub.queue.observe('device-a', observed)
    assert hub.queue.job('device-a', ID)['cancel_requested'] is True


@pytest.mark.parametrize('operation', ['status', 'check'])
@pytest.mark.asyncio
async def test_live_status_retries_a_lost_cancel_for_the_same_job(hub, operation):
    submitted = await hub.request('device-a', START)
    device_job = {**submitted['job'], 'phase': 'downloading', 'cancel_requested': False}
    hub.queue.observe('device-a', device_job)
    hub.peers['device-a'] = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    hub._request = AsyncMock(side_effect=UpdateChannelError('outcome_pending'))
    with pytest.raises(UpdateChannelError):
        await hub.request('device-a', {'op': 'cancel', 'job_id': ID})
    calls = []

    async def respond(machine, command):
        calls.append(command)
        if command['op'] == operation:
            return {**STATUS, 'job': device_job}
        assert command == {'op': 'cancel', 'job_id': ID}
        return {'ok': True, 'job': {**device_job, 'cancel_requested': True}}

    hub._request = respond
    result = await hub.request('device-a', {'op': operation})
    assert calls == [{'op': operation}, {'op': 'cancel', 'job_id': ID}]
    assert result['job']['cancel_requested'] is True
    assert hub.queue.job('device-a', ID)['cancel_requested'] is True


@pytest.mark.asyncio
async def test_lost_cancel_survives_poll_failure_and_relay_restart(hub):
    submitted = await hub.request('device-a', START)
    device_job = {**submitted['job'], 'phase': 'downloading', 'cancel_requested': False}
    hub.queue.observe('device-a', device_job)
    hub.peers['device-a'] = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    hub._request = AsyncMock(side_effect=UpdateChannelError('outcome_pending'))
    with pytest.raises(UpdateChannelError):
        await hub.request('device-a', {'op': 'cancel', 'job_id': ID})
    hub._request = AsyncMock(side_effect=[{**STATUS, 'job': device_job}, UpdateChannelError('outcome_pending')])
    with pytest.raises(UpdateChannelError):
        await hub.request('device-a', {'op': 'status'})
    assert hub.queue.job('device-a', ID)['cancel_requested'] is True
    restored = UpdateRelay(DeviceStore(str(hub.queue.devices.path)))
    peer = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    restored.peers['device-a'] = peer
    restored._request = AsyncMock(return_value={'ok': True, 'job': {**device_job, 'phase': 'cancelled'}})
    await restored._deliver('device-a', peer)
    restored._request.assert_awaited_once_with('device-a', {'op': 'cancel', 'job_id': ID})
    assert restored.queue.job('device-a', ID)['phase'] == 'cancelled'


@pytest.mark.asyncio
async def test_lookup_preserves_pending_cancel_without_forwarding_commands(hub):
    submitted = await hub.request('device-a', START)
    device_job = {**submitted['job'], 'phase': 'downloading', 'cancel_requested': False}
    hub.queue.observe('device-a', device_job)
    await hub.request('device-a', {'op': 'cancel', 'job_id': ID})
    hub.peers['device-a'] = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    command = {'op': 'lookup', 'job_id': ID}
    hub._request = AsyncMock(return_value={'ok': True, 'job_id': ID, 'receipt': 'found', 'job': device_job})
    result = await hub.request('device-a', command)
    assert result['job']['cancel_requested'] is True
    hub._request.assert_awaited_once_with('device-a', command)


@pytest.mark.asyncio
async def test_device_acknowledgement_stops_cancel_retries_during_polling(hub):
    submitted = await hub.request('device-a', START)
    device_job = {**submitted['job'], 'phase': 'downloading', 'cancel_requested': False}
    hub.queue.observe('device-a', device_job)
    await hub.request('device-a', {'op': 'cancel', 'job_id': ID})
    acknowledged = {**device_job, 'cancel_requested': True}
    hub.peers['device-a'] = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    hub._request = AsyncMock(return_value={**STATUS, 'job': acknowledged})
    assert (await hub.request('device-a', {'op': 'status'}))['job']['cancel_requested'] is True
    hub._request.assert_awaited_once_with('device-a', {'op': 'status'})


@pytest.mark.parametrize('phase', ['cancelled', 'succeeded', 'failed'])
@pytest.mark.asyncio
async def test_terminal_device_receipt_settles_pending_cancellation(hub, phase):
    submitted = await hub.request('device-a', START)
    device_job = {**submitted['job'], 'phase': 'downloading', 'cancel_requested': False}
    hub.queue.observe('device-a', device_job)
    await hub.request('device-a', {'op': 'cancel', 'job_id': ID})
    hub.peers['device-a'] = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    hub._request = AsyncMock(return_value={**STATUS, 'job': {**device_job, 'phase': phase}})
    result = await hub.request('device-a', {'op': 'status'})
    assert result['job']['phase'] == phase
    assert hub.queue.job('device-a', ID)['cancel_requested'] is False
    hub._request.assert_awaited_once_with('device-a', {'op': 'status'})


@pytest.mark.asyncio
async def test_rejected_cancellation_keeps_the_relay_intent_pending(hub):
    submitted = await hub.request('device-a', START)
    hub.queue.observe('device-a', {**submitted['job'], 'phase': 'downloading'})
    peer = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    hub.peers['device-a'] = peer
    hub._request = AsyncMock(return_value={'ok': False, 'reason': 'busy'})
    result = await hub.request('device-a', {'op': 'cancel', 'job_id': ID})
    assert result['job']['phase'] == 'unknown' and result['job']['cancel_requested'] is True
    assert (await hub.request('device-a', {**START, 'job_id': 'b' * 32}))['reason'] == 'busy'
    hub._request = AsyncMock(return_value={'ok': True, 'job': {**submitted['job'], 'phase': 'cancelled'}})
    await hub._deliver('device-a', peer)
    hub._request.assert_awaited_once_with('device-a', {'op': 'cancel', 'job_id': ID})


@pytest.mark.asyncio
async def test_nonterminal_cancel_response_cannot_clear_the_user_intent(hub):
    submitted = await hub.request('device-a', START)
    hub.queue.observe('device-a', {**submitted['job'], 'phase': 'downloading'})
    hub.peers['device-a'] = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    hub._request = AsyncMock(return_value={'ok': True, 'job': {
        **submitted['job'], 'phase': 'installing', 'cancel_requested': False,
    }})
    result = await hub.request('device-a', {'op': 'cancel', 'job_id': ID})
    assert result['job']['phase'] == 'installing' and result['job']['cancel_requested'] is True


@pytest.mark.asyncio
async def test_a_device_cancellation_fence_settles_a_lost_start(hub):
    await hub.request('device-a', START)
    state = hub.queue.job('device-a', ID)
    hub.queue.save('device-a', {**state, 'delivered': True})
    await hub.request('device-a', {'op': 'cancel', 'job_id': ID})
    peer = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    hub.peers['device-a'] = peer
    hub._request = AsyncMock(return_value={'ok': True, 'job': None})
    await hub._deliver('device-a', peer)
    assert hub.queue.job('device-a', ID)['phase'] == 'cancelled'


@pytest.mark.asyncio
async def test_definitive_device_rejection_releases_the_relay_intent_for_retry(hub):
    hub.peers['device-a'] = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    hub._request = AsyncMock(side_effect=[STATUS, {'ok': False, 'reason': 'busy'}])
    result = await hub.request('device-a', START)
    assert result['reason'] == 'busy'
    assert hub.queue.job('device-a', ID)['phase'] == 'blocked'
    following = {**START, 'job_id': 'b' * 32}
    hub._request = AsyncMock(side_effect=[STATUS, {'ok': True, 'job': {
        'id': following['job_id'], 'version': following['version'], 'previous': STATUS['current'], 'phase': 'downloading',
    }}])
    assert (await hub.request('device-a', following))['job']['phase'] == 'downloading'


@pytest.mark.asyncio
async def test_an_older_receipt_settles_without_replacing_another_active_job(hub):
    accepted = await hub.request('device-a', START)
    other = {**accepted['job'], 'id': 'b' * 32, 'phase': 'waiting'}
    hub.peers['device-a'] = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    hub._request = AsyncMock(side_effect=[{**STATUS, 'job': other},
                                         {'ok': True, 'job': {**accepted['job'], 'phase': 'failed'}}])
    result = await hub.request('device-a', {'op': 'status'})
    assert result['job']['id'] == other['id']
    assert hub.queue.job('device-a', ID)['phase'] == 'failed'


@pytest.mark.asyncio
async def test_relay_receipt_confirms_accepted_intent_before_device_has_recorded_it(hub):
    await hub.request('device-a', START)
    hub.peers['device-a'] = Peer(SimpleNamespace(), AsyncMock(return_value=True))
    hub._request = AsyncMock(return_value={'ok': True, 'job_id': ID, 'receipt': 'missing', 'job': None})
    result = await hub.request('device-a', {'op': 'lookup', 'job_id': ID})
    assert result['receipt'] == 'found' and result['job']['phase'] == 'waiting_device'


def test_offline_queue_never_stores_paths_or_nonpublic_device_state(hub):
    hub.queue.remember('device-a', {**STATUS, 'secret': 'private', 'path': '/private/path'})
    assert 'private' not in json.dumps(hub.queue.status('device-a'))


def test_authenticated_offline_update_is_delivered_over_the_independent_socket(tmp_path):
    server._login_limiter.reset()
    cfg = _cfg(tmp_path)
    app = server.create_app(cfg)
    app.state.updates.queue.remember('device-a', STATUS)
    with TestClient(app, base_url=cfg.public_origin) as client:
        assert client.post('/api/login', json={'password': cfg.login_password}).status_code == 200
        accepted = client.post('/api/updates/device-a', json=START, headers={'Origin': cfg.public_origin})
        assert accepted.status_code == 200 and accepted.json()['job']['phase'] == 'waiting_device'
        with client.websocket_connect('/ws/updates', headers={'Authorization': f'Bearer {cfg.wrapper_token}'}) as peer:
            peer.send_json({'schema': 1, 'machine_id': 'device-a'})
            assert peer.receive_json() == {'schema': 1, 'ready': True}
            request = peer.receive_json()
            assert {key: request[key] for key in START} == START
            peer.send_json({'schema': 1, 'id': request['id'], 'result': {
                'ok': True, 'job': {**accepted.json()['job'], 'phase': 'downloading'},
            }})
            with ThreadPoolExecutor(1) as pool:
                status = pool.submit(client.get, '/api/updates/device-a')
                request = peer.receive_json()
                assert request['op'] == 'status'
                peer.send_json({'schema': 1, 'id': request['id'], 'result': {
                    **STATUS, 'job': {**accepted.json()['job'], 'phase': 'waiting_network'},
                }})
                result = status.result(timeout=3)
                assert result.status_code == 200 and result.json()['job']['phase'] == 'waiting_network'
