from scripts.live.update_acceptance.native_sessions import observe_marker, observe_history


def test_reasoning_and_quoted_failure_cannot_pass_native_acceptance():
    row = {}
    for channel, text in [('thinking', 'CCR_UPDATE_DSH_OK'),
                          ('commentary', 'CCR_UPDATE_DSH_OK'),
                          ('final', 'I cannot report CCR_UPDATE_DSH_OK because the command failed.')]:
        observe_marker(row, 'dsh', {'type': 'delta', 'message_id': channel,
                                   'channel': channel, 'text': text})
        assert not row['marker']
    observe_marker(row, 'dsh', {'type': 'delta', 'message_id': 'actual-answer',
                               'channel': 'final', 'text': 'CCR_UPDATE_DSH_'})
    assert not row['marker']
    observe_marker(row, 'dsh', {'type': 'delta', 'message_id': 'actual-answer',
                               'channel': 'final', 'text': 'OK'})
    assert row['marker']


def test_probe_records_real_outcome_and_cannot_overwrite_an_existing_run(tmp_path):
    import json
    from pathlib import Path
    import shutil
    import subprocess
    import sys

    script = tmp_path / 'native_probe.py'
    shutil.copyfile(Path(__file__).parents[1] / 'scripts/live/update_acceptance/native_probe.py', script)
    failed = subprocess.run([sys.executable, str(script), 'dsh', '--seconds', '1'], capture_output=True)
    assert failed.returncode != 0
    assert json.loads((tmp_path / 'dsh-result.json').read_text())['success'] is False
    (tmp_path / 'finish-claude').touch()
    success = subprocess.run([sys.executable, str(script), 'claude', '--seconds', '1'], capture_output=True)
    assert success.returncode == 0
    receipt = (tmp_path / 'claude-result.json').read_bytes()
    assert json.loads(receipt)['success'] is True
    duplicate = subprocess.run([sys.executable, str(script), 'claude', '--seconds', '1'], capture_output=True)
    assert duplicate.returncode != 0
    assert (tmp_path / 'claude-result.json').read_bytes() == receipt


def test_unknown_stream_requires_final_channel_confirmation_and_exact_queue_answers():
    row = {'queue_observed': True}
    observe_marker(row, 'claude-queue', {'type': 'delta', 'message_id': 'one',
                                       'text': 'CCR_UPDATE_CLAUDE-QUEUE_OK'})
    assert not row['marker']
    observe_marker(row, 'claude-queue', {'type': 'assistant_msg_end', 'message_id': 'one', 'channel': 'final'})
    assert not row['marker']
    observe_marker(row, 'claude-queue', {'type': 'delta', 'message_id': 'two',
                                       'channel': 'final', 'text': 'CCR_UPDATE_QUEUED_OK'})
    assert row['marker']


def test_completed_message_replay_is_idempotent_without_resetting_dsh_partial_starts():
    row = {}
    for _reconnect in range(3):
        for text in ['CCR_UPDATE_', 'CODEX_OK']:
            observe_marker(row, 'codex', {'type': 'assistant_msg_start', 'message_id': 'answer', 'channel': 'final'})
            observe_marker(row, 'codex', {'type': 'delta', 'message_id': 'answer', 'text': text})
        observe_marker(row, 'codex', {'type': 'assistant_msg_end', 'message_id': 'answer', 'channel': 'final'})
        assert row['marker'] and row['answer'] == 'CCR_UPDATE_CODEX_OK'


def test_cold_history_accepts_only_exact_submitted_final_completion():
    import copy
    event = {'type': 'history', 'authoritative': True, 'session_id': 'session', 'revision': 'revision',
             'turns': [{'id': 'native', 'clientMsgId': 'browser', 'done': True,
                        'blocks': [{'kind': 'text', 'message_id': 'answer', 'done': True,
                                    'channel': 'final', 'text': 'CCR_UPDATE_DSH_OK'}]}]}
    for case in ('other-session', 'other-user', 'non-authoritative', 'running', 'interrupted', 'reasoning'):
        changed = copy.deepcopy(event)
        turn = changed['turns'][0]
        if case == 'other-session':
            changed['session_id'] = 'other'
        if case == 'other-user':
            turn['clientMsgId'] = 'other'
        if case == 'non-authoritative':
            changed['authoritative'] = False
        if case == 'running':
            turn['done'] = False
        if case == 'interrupted':
            turn['interrupted'] = True
        if case == 'reasoning':
            turn['blocks'][0]['channel'] = 'thinking'
        row = {'sid': 'session', 'msg_id': 'browser'}
        observe_history(row, 'dsh', changed)
        assert not (row.get('terminal') and row.get('marker')), case
    row = {'sid': 'session', 'msg_id': 'browser'}
    observe_history(row, 'dsh', event)
    assert row['terminal'] and row['marker']
