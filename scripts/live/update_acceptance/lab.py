#!/usr/bin/env python3
"""Exercise the real UI update RPC in explicitly configured disposable Docker installations."""
from __future__ import annotations

import argparse
import http.client
import http.cookiejar
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPCookieProcessor, ProxyHandler, Request, build_opener
import uuid

TERMINAL = {'succeeded', 'failed', 'blocked', 'cancelled'}


def command(args, *, input=None, timeout=45):
    return subprocess.run(args, input=input, text=True, capture_output=True, check=True, timeout=timeout).stdout


class Api:
    def __init__(self, config, ca):
        self.config = config
        context = ssl.create_default_context(cadata=ca)
        relay_ip = config['relay_ip']
        class Connection(http.client.HTTPSConnection):
            def connect(self):
                raw = socket.create_connection((relay_ip, 443), timeout=self.timeout)
                self.sock = context.wrap_socket(raw, server_hostname=self.host)
        class Handler(HTTPSHandler):
            def https_open(self, req):
                return self.do_open(Connection, req)
        self.client = build_opener(ProxyHandler({}), Handler(context=context), HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.logins = 0

    def call(self, path, data=None, *, authenticate=True):
        req = Request(self.config['origin'] + path, json.dumps(data).encode() if data is not None else None,
                      headers={'Origin': self.config['origin'], 'Content-Type': 'application/json'})
        try:
            with self.client.open(req, timeout=45) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code == 401 and authenticate:
                self.login()
                return self.call(path, data, authenticate=False)
            try:
                return json.load(exc)
            except ValueError:
                return {'reason': 'http_' + str(exc.code)}

    def login(self):
        self.logins += 1
        return self.call('/api/login', {'password': self.config['password']}, authenticate=False)


class Lab:
    def __init__(self, config, evidence):
        self.config = config
        self.evidence = evidence
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.prefix = config.get('prefix', 'ccr-update-lab')
        if config.get('isolated') is not True or urlsplit(config['origin']).hostname != 'relay.ccr.test':
            raise ValueError('this runner only accepts the isolated .test fixture')
        for key in ['relay_ip', 'wrapper_ip', 'fixture_ip']:
            if not ipaddress.ip_address(config[key]).is_private:
                raise ValueError('acceptance containers require private addresses')
        self.api = None

    def name(self, role):
        return self.prefix + '-' + role

    def execute(self, role, args, **kw):
        return command(['docker', 'exec', '-i', self.name(role), *args], **kw)

    def python(self, role, code, **kw):
        return self.execute(role, ['python3', '-'], input=code, **kw)

    def inspect(self, role):
        return json.loads(command(['docker', 'inspect', self.name(role)]))[0]

    def owned(self, role):
        value = self.inspect(role)
        label = (value['Config'].get('Labels') or {}).get('cc-remote.acceptance')
        if label != 'update' and value['Id'] not in self.config.get('initial_container_ids', []):
            raise ValueError('refusing to touch a container without acceptance ownership')
        return value

    def reset(self):
        # Do not erase an unresolved scenario or stop its surviving installer.
        self.wait_installers()
        original = json.loads(self.python('wrapper', "from pathlib import Path\nimport json\nr=Path('/opt/cc-remote-wrapper/ui-update-jobs');a=r/'active.json';v=json.loads(a.read_text()) if a.exists() else None\nprint(json.dumps(json.loads((r/(v['id']+'.json')).read_text()) if v else None))\n"))
        if original and original['phase'] not in TERMINAL:
            raise RuntimeError('original update is unresolved; inspect it instead of resetting the lab')
        for role in ['wrapper', 'relay']:
            self.owned(role)
            command(['docker', 'rm', '-f', self.name(role)])
        for role in ['relay', 'wrapper']:
            image = f'cc-remote-update-lab-{role}:fixed-baseline'
            command(['docker', 'run', '-d', '--name', self.name(role), '--label', 'cc-remote.acceptance=update',
                     '--network', self.config.get('network', 'ccr-update-lab'), '--ip', self.config[role + '_ip'],
                     '--add-host', 'relay.ccr.test:' + self.config['relay_ip'],
                     '--add-host', 'github.com:' + self.config['fixture_ip'],
                     '--add-host', 'api.github.com:' + self.config['fixture_ip'],
                     '--privileged', '--cgroupns', 'private', '--tmpfs', '/run', '--tmpfs', '/run/lock',
                     '--tmpfs', '/tmp:exec,mode=1777', '-e', 'HOME=/root', '-e', 'USER=root', image])
        self.python('fixture', "from pathlib import Path\nPath('/fixture/faults.json').write_text('{}')\n")
        self.ready()

    def ready(self):
        ca = self.execute('relay', ['cat', '/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt'])
        self.api = Api(self.config, ca)
        deadline = time.monotonic() + 90
        last = None
        while time.monotonic() < deadline:
            try:
                state = self.api.call('/healthz')
                if self.config['machine'] in state.get('machines', []):
                    status = self.status()
                    if status.get('enabled') and not status.get('maintenance'):
                        return status
                last = state
            except (OSError, ValueError) as exc:
                last = type(exc).__name__
            time.sleep(1)
        raise RuntimeError(f'baseline did not become ready: {last}')

    def status(self):
        return self.api.call('/api/updates/' + self.config['machine'])

    def request(self, data):
        return self.api.call('/api/updates/' + self.config['machine'], data)

    def fault_tls(self):
        value = {'tls': {'remaining': 1}}
        self.python('fixture', "from pathlib import Path\nPath('/fixture/faults.json').write_text(" + repr(json.dumps(value)) + ')\n')

    def wait_installers(self):
        for role, root in [('wrapper', '/opt/cc-remote-wrapper'), ('relay', '/opt/cc-remote')]:
            self.owned(role)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                value = self.execute(role, [root + '/current/.venv/bin/python', '-I', '-c',
                    "import sys;sys.path.insert(0," + repr(root + '/current') + ");from cc_remote.activation import install_locked;from pathlib import Path;print(install_locked(Path(" + repr(root) + ")))" ]).strip()
                if value == 'False':
                    break
                time.sleep(1)
            else:
                raise RuntimeError('original installer is still active; preserve the scenario')

    def fault_ssh(self, scenario):
        source = Path(__file__).with_name('ssh_fault.py').read_text()
        mode = {'ssh-verify': ('verify', 'before'), 'ssh-submit': ('submit', 'before'),
                'ssh-ack': ('submit', 'lost-ack'), 'ssh-status': ('status', 'before')}[scenario]
        self.python('relay', "from pathlib import Path\nimport pwd,os,json\nu=pwd.getpwnam('tester')\n"
                    "p=Path('/home/tester/ccr-ssh-fault.py');p.write_text(" + repr(source) + ");p.chmod(0o755);os.chown(p,u.pw_uid,u.pw_gid)\n"
                    "folder=Path('/home/tester/ccr-acceptance');folder.mkdir(mode=0o700,exist_ok=True);os.chown(folder,u.pw_uid,u.pw_gid)\n"
                    "p=folder/'ssh.json';p.write_text(" + repr(json.dumps({'kind': mode[0], 'mode': mode[1], 'remaining': 1})) + ");os.chown(p,u.pw_uid,u.pw_gid)\n"
                    "p=Path('/home/tester/.ssh/authorized_keys');key=p.read_text().strip();p.write_text('command=\"/home/tester/ccr-ssh-fault.py\",restrict '+key+'\\n')\n")

    def fault_activation(self, scenario, identity):
        self.execute('wrapper', ['runuser', '-u', 'tester', '--', 'python3', '-'], input="from pathlib import Path\nimport sqlite3\np=Path.home()/'.claude/cc-remote/work';p.mkdir(parents=True,exist_ok=True)\nf=p/'registry.sqlite3'\nwith sqlite3.connect(f) as db:\n db.execute('create table if not exists acceptance_canary (value text)');db.execute('delete from acceptance_canary');db.execute(\"insert into acceptance_canary values ('before')\")\nf.chmod(0o600)\n")
        gate = "#!/usr/bin/python3\nfrom pathlib import Path\nimport json,time\nr=Path('/opt/cc-remote-wrapper')\nif json.loads((r/'current/release-manifest.json').read_text())['product_version']=='5.3.19' and (r/'acceptance-start.json').exists():\n time.sleep(" + ('2' if scenario == 'startup-failure' else '10') + ")\n" + (" raise SystemExit(78)\n" if scenario == 'startup-failure' else '')
        watcher = Path(__file__).with_name('watch_activation.py').read_text()
        self.python('wrapper', "from pathlib import Path\np=Path('/usr/local/libexec/ccr-acceptance-start');p.write_text(" + repr(gate) + ");p.chmod(0o755)\n"
                    "p=Path('/etc/systemd/system/cc-remote-wrapper.service.d');p.mkdir(exist_ok=True);(p/'acceptance.conf').write_text('[Service]\\nExecStartPre=/usr/local/libexec/ccr-acceptance-start\\n')\n"
                    "Path('/opt/cc-remote-wrapper/acceptance-start.json').write_text('{}')\n"
                    "Path('/opt/ccr-watch-activation.py').write_text(" + repr(watcher) + ')\n')
        self.execute('wrapper', ['systemctl', 'daemon-reload'])
        command(['docker', 'exec', '-d', self.name('wrapper'), 'python3', '/opt/ccr-watch-activation.py', identity, scenario])

    def fault_relay(self, scenario):
        gate = "#!/usr/bin/python3\nfrom pathlib import Path\nimport json,time\nr=Path('/opt/cc-remote')\nif json.loads((r/'current/release-manifest.json').read_text())['product_version']=='5.3.19': time.sleep(10)\n"
        watcher = Path(__file__).with_name('watch_relay.py').read_text()
        self.python('relay', "from pathlib import Path\np=Path('/usr/local/libexec/ccr-relay-start');p.parent.mkdir(parents=True,exist_ok=True);p.write_text(" + repr(gate) + ");p.chmod(0o755)\n"
                    "p=Path('/etc/systemd/system/cc-remote-relay.service.d');p.mkdir(exist_ok=True);(p/'acceptance.conf').write_text('[Service]\\nExecStartPre=/usr/local/libexec/ccr-relay-start\\n')\n"
                    "Path('/opt/ccr-watch-relay.py').write_text(" + repr(watcher) + ')\n')
        self.execute('relay', ['systemctl', 'daemon-reload'])
        command(['docker', 'exec', '-d', self.name('relay'), 'python3', '/opt/ccr-watch-relay.py', scenario])

    def capture(self, identity):
        for role, root in [('relay', '/opt/cc-remote'), ('wrapper', '/opt/cc-remote-wrapper')]:
            try:
                data = self.python(role, "from pathlib import Path\nimport json\nr=Path(" + repr(root) + ")\n"
                                   "v={'release':str((r/'current').resolve()),'manifest':json.loads((r/'current/release-manifest.json').read_text())}\n"
                                   "for name in ['activation.json','upstream-update.json']:\n p=r/name\n v[name]=json.loads(p.read_text()) if p.exists() else None\n"
                                   "v['jobs']=[json.loads(p.read_text()) for p in (r/'ui-update-jobs').glob('*.json') if p.name!='active.json']\n"
                                   "v['server_jobs']=[json.loads(p.read_text()) for p in (r/'update-jobs').glob('*.json')]\nprint(json.dumps(v))\n")
                (self.evidence / f'{role}-state.json').write_text(data)
                logs = self.execute(role, ['journalctl', '-u', 'cc-remote-ui-update-*', '-u', 'cc-remote-update-*', '--no-pager', '-o', 'short-iso'], timeout=30)
                (self.evidence / f'{role}-updater.log').write_text(logs)
            except (OSError, subprocess.SubprocessError) as exc:
                (self.evidence / f'{role}-capture-error.txt').write_text(str(exc))
        if identity:
            (self.evidence / 'request-id').write_text(identity + '\n')

    def retry_after_failure(self, report):
        self.python('wrapper', "from pathlib import Path\nPath('/opt/cc-remote-wrapper/acceptance-start.json').unlink(missing_ok=True)\n")
        identity = uuid.uuid4().hex
        result = self.request({'op': 'start', 'job_id': identity, 'version': self.config.get('target', '5.3.19')})
        report['retry'] = {'id': identity, 'submission': result}
        if (result.get('job') or {}).get('id') != identity:
            raise RuntimeError('safe failed attempt could not be retried')
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            try:
                state = self.status()
            except (OSError, ValueError):
                state = {}
            job = state.get('job') or {}
            if job.get('id') == identity and job.get('phase') in TERMINAL:
                report['retry']['final'] = state
                if job['phase'] != 'succeeded' or state.get('maintenance') or state.get('current') != '5.3.19' or state.get('relay_version') != '5.3.19':
                    raise RuntimeError('retry after verified recovery did not install successfully')
                self.wait_installers()
                return
            time.sleep(1)
        raise TimeoutError('retry did not settle')

    def run(self, scenario, *, resume=False):
        if resume:
            report = json.loads((self.evidence / 'report.json').read_text())
            identity = report['id']
            report.setdefault('harness_restarts', []).append({'at': time.time(), 'previous_error': report.pop('error', None)})
            self.ready()
        else:
            self.reset()
            identity = uuid.uuid4().hex
            report = {'scenario': scenario, 'id': identity, 'started': time.time(), 'states': [], 'pass': False,
                      'target_sha': self.config.get('target_sha')}
        if not resume and scenario.startswith('ssh-'):
            self.fault_ssh(scenario)
        elif not resume and scenario == 'tls-retry':
            self.fault_tls()
        elif not resume and scenario == 'download-retry':
            self.python('fixture', "from pathlib import Path\nimport json\nPath('/fixture/faults.json').write_text(json.dumps({'http':{'remaining':1,'path':'/muggle-stack/cc-remote/releases/download/'}}))\n")
        elif not resume and scenario in {'worker-kill', 'installer-kill', 'startup-failure'}:
            self.fault_activation(scenario, identity)
        elif not resume and scenario.startswith('relay-'):
            self.fault_relay(scenario)
        elif not resume and scenario == 'cancel-download':
            self.python('fixture', "from pathlib import Path\nimport json\nPath('/fixture/faults.json').write_text(json.dumps({'delay':{'remaining':1,'seconds':12,'path':'/muggle-stack/cc-remote/releases/download/'}}))\n")
        elif not resume and scenario == 'cancel-delayed-worker':
            self.python('wrapper', "from pathlib import Path\np=Path('/etc/systemd/system/cc-remote-ui-update-" + identity + ".service.d');p.mkdir();(p/'acceptance.conf').write_text('[Service]\\nExecStartPre=/bin/sleep 10\\n')\n")
            self.execute('wrapper', ['systemctl', 'daemon-reload'])
        try:
            request = {'op': 'start', 'job_id': identity, 'version': self.config.get('target', '5.3.19')}
            if not resume and scenario == 'certificate-retry':
                certificate = self.execute('wrapper', ['cat', '/usr/local/share/ca-certificates/ccr-lab-fixture.crt'])
                self.python('wrapper', "from pathlib import Path\nPath('/usr/local/share/ca-certificates/ccr-lab-fixture.crt').unlink()\n")
                self.execute('wrapper', ['update-ca-certificates', '--fresh'])
                report['rejected_certificate'] = self.request(request)
                if report['rejected_certificate'].get('reason') != 'tls_certificate' or self.status().get('maintenance'):
                    raise RuntimeError('certificate failure was not classified without maintenance')
                self.python('wrapper', "from pathlib import Path\nPath('/usr/local/share/ca-certificates/ccr-lab-fixture.crt').write_text(" + repr(certificate) + ')\n')
                self.execute('wrapper', ['update-ca-certificates', '--fresh'])
            if not resume:
                result = self.request(request)
                report['submission'] = result
                if (result.get('job') or {}).get('id') != identity:
                    raise RuntimeError(f'update submission failed: {result}')
            last = None
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                try:
                    state = self.status()
                except (OSError, ValueError):
                    state = {'reason': 'control_reconnecting'}
                key = (state.get('current'), state.get('relay_version'), (state.get('job') or {}).get('phase'), state.get('maintenance'))
                if key != last:
                    report['states'].append({'at': time.time(), **state})
                    print(scenario, key, flush=True)
                    last = key
                job = state.get('job') or {}
                if scenario.startswith('cancel-') and not report.get('cancel') and job.get('phase') in ({'submitting'} if scenario == 'cancel-delayed-worker' else {'downloading'}):
                    report['cancel'] = self.request({'op': 'cancel', 'job_id': identity})
                if job.get('phase') in TERMINAL:
                    if scenario == 'relay-installer-kill':
                        if job['id'] != identity or job['phase'] not in {'failed', 'blocked'} or state.get('maintenance') or state.get('current') != '5.3.18' or state.get('relay_version') != '5.3.18':
                            raise RuntimeError('server rollback did not settle and leave local chat open')
                        report['rollback'] = state
                        report['fault'] = json.loads(self.execute('relay', ['cat', '/opt/cc-remote/acceptance-watch.json']))
                        self.wait_installers()
                        self.retry_after_failure(report)
                        report['pass'] = True
                        break
                    if scenario.startswith('cancel-'):
                        if job['id'] != identity or job['phase'] != 'cancelled' or state.get('maintenance') or state.get('current') != '5.3.18' or state.get('relay_version') != '5.3.18':
                            raise RuntimeError('cancel did not leave the installation usable and unchanged')
                        if scenario == 'cancel-delayed-worker':
                            time.sleep(12)  # Let the real, already accepted OS launch arrive after cancellation.
                            if self.status().get('job', {}).get('phase') != 'cancelled':
                                raise RuntimeError('late OS worker escaped the cancellation fence')
                        report['cancelled'] = state
                        self.wait_installers()
                        self.retry_after_failure(report)
                        report['pass'] = True
                        break
                    if scenario in {'installer-kill', 'startup-failure'}:
                        if job['id'] != identity or job['phase'] != 'failed' or state.get('maintenance') or state.get('current') != '5.3.18':
                            raise RuntimeError(f'interrupted activation was not safely rolled back: {state}')
                        canary = self.python('wrapper', "import sqlite3\nwith sqlite3.connect('/home/tester/.claude/cc-remote/work/registry.sqlite3') as db: print(db.execute('select value from acceptance_canary').fetchone()[0])\n").strip()
                        if canary != 'before':
                            raise RuntimeError('rollback did not restore the actual private database')
                        report['rollback'] = state
                        report['restored_canary'] = canary
                        report['fault'] = json.loads(self.execute('wrapper', ['cat', '/opt/cc-remote-wrapper/acceptance-watch.json']))
                        self.wait_installers()
                        self.retry_after_failure(report)
                        report['pass'] = True
                        break
                    if job['id'] != identity or job['phase'] != 'succeeded' or state.get('maintenance'):
                        raise RuntimeError(f'update ended incorrectly: {state}')
                    if state['current'] != request['version'] or state['relay_version'] != request['version']:
                        raise RuntimeError('terminal result does not match real installed versions')
                    self.wait_installers()
                    for role, root in [('relay', '/opt/cc-remote'), ('wrapper', '/opt/cc-remote-wrapper')]:
                        manifest = json.loads(self.execute(role, ['cat', root + '/current/release-manifest.json']))
                        if manifest['git_sha'] != self.config['target_sha']:
                            raise RuntimeError('active installation differs from the tested source snapshot')
                    if self.execute('wrapper', ['systemctl', 'show', 'cc-remote-wrapper', '--property=NoNewPrivileges', '--value']).strip() != 'yes':
                        raise RuntimeError('the Wrapper lost its existing privilege restriction')
                    receipts = json.loads(self.python('relay', "from pathlib import Path\nimport json\nprint(json.dumps([json.loads(p.read_text()) for p in Path('/opt/cc-remote/update-jobs').glob('*.json')]))\n"))
                    if len(receipts) != 1:
                        raise RuntimeError('server update was not delivered exactly once')
                    report['server_unit'] = receipts[0]['request']['unit']
                    if scenario.startswith('ssh-'):
                        injected = json.loads(self.execute('relay', ['cat', '/home/tester/ccr-acceptance/ssh.json']))
                        if injected['remaining'] != 0:
                            raise RuntimeError('SSH fault was not reached')
                        units = {c['unit'] for c in injected['calls'] if c['kind'] == 'submit'}
                        if units != {report['server_unit']}:
                            raise RuntimeError('SSH reconnect changed the transaction identity')
                        report['fault'] = injected
                    if scenario in {'tls-retry', 'download-retry'}:
                        injected = json.loads(self.execute('fixture', ['cat', '/fixture/faults.json']))
                        if any(v['remaining'] for v in injected.values()):
                            raise RuntimeError('network fault was not reached')
                        report['fault'] = injected
                    if scenario == 'worker-kill':
                        report['fault'] = json.loads(self.execute('wrapper', ['cat', '/opt/cc-remote-wrapper/acceptance-watch.json']))
                        if job.get('id') != report['fault']['id']:
                            raise RuntimeError('controller recovery changed the original request')
                    if scenario == 'relay-worker-kill':
                        report['fault'] = json.loads(self.execute('relay', ['cat', '/opt/cc-remote/acceptance-watch.json']))
                        if report['fault']['unit'] != report['server_unit']:
                            raise RuntimeError('server recovery did not follow the original job')
                    report['pass'] = True
                    break
                time.sleep(1)
            if not report['pass']:
                raise TimeoutError('real update did not settle')
        except Exception as exc:
            report['error'] = str(exc)
            raise
        finally:
            report['finished'] = time.time()
            report['logins'] = self.api.logins
            (self.evidence / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            self.capture(identity)
        return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--scenario', choices=['idle', 'tls-retry', 'download-retry', 'certificate-retry', 'ssh-verify', 'ssh-submit', 'ssh-ack', 'ssh-status', 'worker-kill', 'installer-kill', 'startup-failure', 'cancel-download', 'cancel-delayed-worker', 'relay-worker-kill', 'relay-installer-kill'])
    parser.add_argument('--reset-only', action='store_true')
    parser.add_argument('--resume', action='store_true', help='observe the original recorded request after a harness disconnect')
    args = parser.parse_args()
    lab = Lab(json.loads(args.config.read_text()), args.evidence)
    if args.reset_only:
        lab.reset()
        print('Isolated baseline ready')
    elif args.scenario:
        print(json.dumps(lab.run(args.scenario, resume=args.resume), ensure_ascii=False))
    else:
        parser.error('choose a scenario or --reset-only')
