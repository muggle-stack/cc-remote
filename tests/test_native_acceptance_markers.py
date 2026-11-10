from scripts.live.update_acceptance.native_sessions import observe_marker


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
