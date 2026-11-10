# Live update acceptance

These probes perform actual installations and may restart **disposable test
services**. They are deliberately outside pytest/CI. `native_sessions.py` is a
separate, explicitly authorized model-token test for an in-scope device.

The lab uses genuine systemd, the role installers, Caddy HTTPS, SSH, the actual
Relay/Wrapper update RPC, and the browser UI. It does not replace installer,
process, network, or activation responses with mocks.

## Disposable installation

Prepare a private Docker network containing three containers: `PREFIX-relay`,
`PREFIX-wrapper`, and `PREFIX-fixture`. Use real Linux systemd as PID 1 for the
first two, private cgroup namespaces and executable `/tmp`. Install the baseline
using the repository role bundles. Keep actual user credentials, model account
homes, production volumes and the Docker socket out of these containers.

Configure the Relay at `https://relay.ccr.test`, enroll the test Wrapper, enable
its UI updater and authorize its test-account SSH route to the test Relay.
Trust a test CA only in these containers. Map `github.com` and `api.github.com`
to the fixture IP only inside the test containers. `release_server.py` serves
the exact stable-release API and artifact paths; use genuine repository-built
bundles, matching Web metadata and `SHA256SUMS`. Never publish synthetic versions.

After verifying baseline services and idle installers, commit baseline images
as `cc-remote-update-lab-{relay,wrapper}:fixed-baseline`. Label owned containers
`cc-remote.acceptance=update`. The runner refuses to reset an unresolved update.
Keep a private mode-0600 configuration outside the repository:

```json
{
  "isolated": true,
  "prefix": "ccr-update-lab",
  "network": "ccr-update-lab",
  "relay_ip": "172.30.0.2",
  "wrapper_ip": "172.30.0.3",
  "fixture_ip": "172.30.0.4",
  "origin": "https://relay.ccr.test",
  "password": "TEST-ONLY-LOGIN-PASSWORD",
  "machine": "ENROLLED-TEST-DEVICE-ID",
  "target": "5.3.19",
  "target_sha": "EXACT-TESTED-SOURCE-SHA"
}
```

Run one case at a time. Do not reset the same containers from concurrent tests.

```bash
python3 scripts/live/update_acceptance/lab.py --config PRIVATE_CONFIG \
  --evidence PRIVATE_EVIDENCE/idle --scenario idle
```

Cases: `idle`, `tls-retry`, `download-retry`, `certificate-retry`, `ssh-verify`,
`ssh-submit`, `ssh-ack`, `ssh-status`, `worker-kill`, `installer-kill`,
`startup-failure`, `cancel-download`, `cancel-delayed-worker`,
`relay-worker-kill`, `relay-installer-kill`.

The failure cases verify actual rollback, private SQLite data restoration and
successful retry. Cancellation verifies that even an already accepted but delayed
OS worker cannot install after cancellation. Successful updates require exact
installed manifests, a single server transaction and `maintenance=false`.
Evidence includes receipts, original request IDs, state transitions and journals.
If the runner disconnects, inspect that evidence and use `--resume` to follow the
same request. Do not launch another scenario over an unresolved transaction.

For browser acceptance, reset to baseline, make `relay.ccr.test` reachable from an
isolated Playwright context (an optional `--proxy` can provide that route), then:

```bash
node scripts/live/update_acceptance/browser.mjs --config PRIVATE_CONFIG \
  --evidence PRIVATE_EVIDENCE/browser --proxy http://127.0.0.1:PRIVATE_PORT
```

This clicks the real update control, repeats the same request, forces relogin,
checks in-memory draft retention, and verifies completion and input availability.
Run `--reload` separately after another baseline reset to verify full-page reload
and original-job recovery. Drafts intentionally do not persist across a manual
full reload. Certificate exceptions are confined to the disposable browser
context; installers still verify HTTPS certificates.

## Native model continuity

After explicit authorization, place `native_probe.py` in a dedicated directory
on the in-scope device. Run `native_sessions.py` from an independent Relay-side
controller with a private configuration containing `authorized_live_models:true`,
`origin`, `login_env`, `machine`, `protocol`, `cwd`, `engines`, and optional native
session `options`. It reads existing authentication locally without exporting it.

The driver creates one temporary session per selected engine and submits each
prompt only once. Wait for all heartbeat files before activating a staged release
with the normal deployment procedure. Capture independent native service/process
identities before and after. Release each command by creating `finish-ENGINE` in
the probe directory. Check continuous heartbeat PIDs, each local `ENGINE-result.json`
receipt's `success:true`, standalone success markers in final answers, and native
terminal results. Reasoning, quoted tokens and a model turn's successful completion
do not establish a successful command. Archive only these temporary sessions afterward.
Repeated live replay replaces the same completed answer. For a cold unfocused
session, authoritative History can supply its final answer and terminal state,
but only when its user ID matches this run's exact submitted message. An unrelated
row, open response, interrupted turn or reasoning text cannot pass acceptance.

Optional `claude-queue` adds a real deferred query; release that probe only after
the installer reports waiting for it, then verify the queued query runs before
activation proceeds. Optional `claude-question` holds a native question across
the restart. Create `answer-claude-question` in the Relay evidence directory only
after reconnect, then verify that the same question is answered and completed.
The driver waits for this question before starting timed command probes, so a
slow initial model response does not consume their continuity window.

Container fault tests prove updater recovery. Native probes prove real model
continuity. Report their evidence separately; passing one is not proof of the
other. A source activation is also distinct from a published-Release UI upgrade.
