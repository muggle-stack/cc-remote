#!/usr/bin/env python3
"""Create only explicitly configured temporary sessions and observe through reconnects.

Run on the Relay, using its existing local login environment. This spends model
tokens. Never resubmits a create or prompt after a lost connection. Persisted
state prevents accidentally rerunning the same acceptance operation.
"""
from __future__ import annotations

import argparse
import asyncio
from http.cookiejar import CookieJar
import json
from pathlib import Path
import shlex
import time
from urllib.request import HTTPCookieProcessor, Request, build_opener
import uuid

from dotenv import dotenv_values
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed


async def run(config, evidence):
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    report_path = evidence / 'native-sessions.json'
    if report_path.exists():
        raise ValueError('inspect the original acceptance; never recreate its sessions')
    origin = config['origin'].rstrip('/')
    if not origin.startswith('https://') or not config.get('authorized_live_models'):
        raise ValueError('explicit live-model authorization and HTTPS origin required')
    report = {'started': time.time(), 'sessions': {}, 'connections': 0, 'pass': False}
    for engine in config['engines']:
        if engine not in {'claude', 'codex', 'dsh', 'claude-queue', 'claude-question'}:
            raise ValueError('unsupported engine')
        report['sessions'][engine] = {'request_id': uuid.uuid4().hex, 'msg_id': uuid.uuid4().hex,
                                      'sent': False, 'sid': None, 'terminal': False, 'marker': False}
    def save():
        pending = report_path.with_suffix('.pending')
        pending.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        pending.replace(report_path)
    save()
    cursors, generations = {}, {}
    client = 'update-acceptance-' + uuid.uuid4().hex
    deadline = time.monotonic() + 900
    try:
        while time.monotonic() < deadline:
            jar = CookieJar()
            http = build_opener(HTTPCookieProcessor(jar))
            env = dotenv_values(config['login_env'])
            login = Request(origin + '/api/login', json.dumps({'password': env['LOGIN_PASSWORD']}).encode(),
                            headers={'Origin': origin, 'Content-Type': 'application/json'})
            try:
                with http.open(login, timeout=15) as response:
                    assert response.status == 200
                cookie = '; '.join(f'{row.name}={row.value}' for row in jar)
                async with connect(origin.replace('https://', 'wss://', 1) + '/ws?machine=' + config['machine'],
                                   origin=origin, additional_headers={'Cookie': cookie}, host='127.0.0.1', port=443,
                                   open_timeout=15, max_size=32 * 1024 * 1024, max_queue=512) as ws:
                    report['connections'] += 1
                    async def send(value):
                        await ws.send(json.dumps({'v': config['protocol'], 'client_id': client, **value}))
                    await send({'type': 'hello', 'role': 'client', 'cursors': cursors, 'generations': generations})
                    for engine, row in report['sessions'].items():
                        if row['sent']:
                            continue
                        command = shlex.join(['python3', config['cwd'] + '/native_probe.py', engine])
                        prompt = ('This is an authorized isolated update-continuity acceptance. Run exactly one foreground shell command: '
                                  + command + '. Allow up to 300 seconds for this command. Do not start a background task or any subagent. '
                                  'Do not inspect or edit other files. After the command finishes, reply only CCR_UPDATE_' + engine.upper() + '_OK.')
                        if engine == 'claude-question':
                            prompt = ('This is an authorized question-recovery acceptance. Use AskUserQuestion to ask exactly one question: '
                                      'Continue the update acceptance? Offer Continue and Stop. Wait for the answer. Do not run commands '
                                      'or spawn agents. After Continue, reply only CCR_UPDATE_CLAUDE-QUESTION_OK.')
                        row['sent'] = True
                        save()  # A disconnect after this point is an unknown create, never permission to resend.
                        await send({'type': 'new_session', 'request_id': row['request_id'], 'cmd_id': row['request_id'],
                                    'msg_id': row['msg_id'], 'cwd': config['cwd'], 'engine': engine.split('-')[0],
                                    'prompt': prompt, **config.get('options', {}).get(engine.split('-')[0], {})})
                    last_history = 0.0
                    while time.monotonic() < deadline:
                        if time.monotonic() - last_history > 10:
                            for row in report['sessions'].values():
                                if row['sid'] and not row['sid'].startswith('tmp-'):
                                    await send({'type': 'get_history', 'session_id': row['sid'], 'detail': 'summary'})
                            last_history = time.monotonic()
                        question = report['sessions'].get('claude-question')
                        if question and question.get('ask') and not question.get('answered') and (evidence / 'answer-claude-question').exists():
                            question['answered'] = time.time()
                            save()
                            ask = question['ask']
                            answer = {'answers': ['Continue'] * len(ask['questions'])} if ask.get('questions') else {'answer': 'Continue'}
                            await send({'type': 'answer_question', 'sid': question['sid'], 'ask_id': ask['ask_id'], **answer})
                        try:
                            event = json.loads(await asyncio.wait_for(ws.recv(), 5))
                        except asyncio.TimeoutError:
                            continue
                        if event.get('type') == 'session_focus':
                            for row in report['sessions'].values():
                                if event.get('request_id') == row['request_id']:
                                    row['sid'] = event['session_id']
                        if event.get('type') == 'session_rekey':
                            for row in report['sessions'].values():
                                if row['sid'] == event['old_key']:
                                    row['sid'] = event['session_id']
                        for engine, row in report['sessions'].items():
                            if event.get('type') == 'error' and event.get('request_id') == row['request_id']:
                                raise RuntimeError('temporary session rejected: ' + str(event.get('code')))
                            if not row['sid'] or event.get('sid', event.get('session_id')) != row['sid']:
                                continue
                            sid = row['sid']
                            if event.get('generation') and event['generation'] != generations.get(sid):
                                cursors[sid] = 0
                            if isinstance(event.get('seq'), int):
                                cursors[sid] = max(cursors.get(sid, 0), event['seq'])
                            if event.get('generation'):
                                generations[sid] = event['generation']
                            with (evidence / (engine + '-events.jsonl')).open('a') as output:
                                output.write(json.dumps({'observed_at': time.time(), **event}, ensure_ascii=False) + '\n')
                            if event['type'] == 'ask_user' and engine == 'claude-question':
                                row['ask'] = event
                                row.setdefault('ask_observations', []).append({'at': time.time(), 'ask_id': event['ask_id'], 'connection': report['connections']})
                            if engine == 'claude-queue' and event['type'] == 'tool_use' and not row.get('queued'):
                                row['queued'] = uuid.uuid4().hex
                                save()
                                await send({'type': 'query', 'sid': sid, 'cmd_id': row['queued'], 'msg_id': row['queued'],
                                            'delivery': 'queue', 'prompt': 'Reply only CCR_UPDATE_QUEUED_OK. Do not use tools.'})
                            if engine == 'claude-queue' and event['type'] == 'query_queue' and event.get('total_count', len(event.get('items', []))):
                                row['queue_observed'] = time.time()
                            if event['type'] == 'turn_end':
                                row['terminal'] = True
                                row['result'] = event['result']
                                if event['result']['is_error']:
                                    raise RuntimeError(engine + ' ended with an error')
                            if event['type'] == 'user_msg':
                                row['terminal'] = False
                            if event['type'] in {'delta', 'history'} and 'CCR_UPDATE_' + engine.upper() + '_OK' in json.dumps(event):
                                # History can contain the original prompt, so only live answer deltas establish this marker.
                                if event['type'] == 'delta':
                                    row['marker'] = True
                            if event['type'] == 'delta':
                                row['answer'] = (row.get('answer', '') + event.get('text', ''))[-8192:]
                                row['marker'] = 'CCR_UPDATE_' + engine.upper() + '_OK' in row['answer']
                                if engine == 'claude-queue':
                                    row['marker'] = row['marker'] and 'CCR_UPDATE_QUEUED_OK' in row['answer'] and bool(row.get('queue_observed'))
                        save()
                        if all(row['terminal'] and row['marker'] for row in report['sessions'].values()):
                            report['pass'] = True
                            report['finished'] = time.time()
                            save()
                            return
            except (OSError, ConnectionClosed, TimeoutError) as exc:
                report.setdefault('reconnects', []).append({'at': time.time(), 'error': type(exc).__name__})
                save()
                await asyncio.sleep(2)
        raise TimeoutError('native acceptance did not finish')
    except Exception as exc:
        report['error'] = str(exc)
        save()
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(json.loads(args.config.read_text()), args.evidence))
