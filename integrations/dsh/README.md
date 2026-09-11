# DeepSeek Harness integration

cc-remote supports `@deepseek-ai/dsh@0.1.5-rc.2` as an optional **Code** engine.
Use the DSH entry in the header to discover sessions on the selected device or
create one with a native Agent Preset. Models, reasoning levels (including
`off`), permission presets, commands and Skills come from that DSH installation.
An unavailable engine shows a connection explanation and an empty model list.

## Connect a local DSH

Use Node 24 and the pinned DSH release. Configure model accounts, tools and
plugins in DSH itself. cc-remote does not start DSH, copy model credentials,
or replace its authentication. Model controls use DSH's native selection API.

Add this entry to an operator-owned DSH patch file, using the **absolute path**
to the bridge from this checkout or the Wrapper release:

```yaml
- insert:
    - id: cc-remote-history
      name: /absolute/path/to/cc-remote/integrations/dsh/cc-remote.mjs
```

Start DSH with its Web profile and that patch:

```sh
dsh --profile web --patch /absolute/path/to/cc-remote-dsh.patch.yml --no-open
```

DSH prints its local login URL. On the same device, use the Wrapper's Python
environment to pair:

```sh
python -m cc_remote.wrapper.dsh_pair --file /private/path/dsh-connection.json
```

Enter the login URL at the hidden prompt. The command exchanges it for DSH's
scoped Cookie and writes a mode-0600 connection file. Only literal loopback HTTP
addresses with explicit ports are accepted. Neither the URL nor the Cookie is
sent to cc-remote's browser or relay. Pairing creates no session or model turn.

Set this in the Wrapper's external configuration, then restart the Wrapper
through the normal release procedure:

```sh
CC_REMOTE_DSH_CONNECTION_FILE=/private/path/dsh-connection.json
```

The connection file contains control authentication. Keep it outside source,
release directories and shared storage. When the Cookie expires, pair again to
the same address; the next request/reconnect reloads the file. Changing the
address requires restarting the Wrapper so an existing session cannot silently
switch to a different DSH instance. Stopping the Wrapper closes subscriptions;
it does not stop native Agents. The Stop button explicitly calls native cancel.

## Controls

- **Conversation:** text, images, file uploads, streaming text/reasoning/tool
  results, steer while running, durable Wrapper-owned queues and cancellation.
- **Interactions:** scoped native questions and one-time tool approvals. Native
  cancellation closes the matching question; history does not reopen it.
- **Goals:** objective, native phase, rounds consumed/limit, and whether automatic
  continuation is armed. Pause, resume, edit and clear use native `/goal`
  commands. Opening history never resumes a goal.
- **Commands and Skills:** the command menu is read from the active Agent;
  commands accepting attachments receive the selected images/files. Failed or
  ambiguous commands preserve the draft. `/model`, `/context`, `/diff`, `/open`
  and `/preview` use the shared interface. Skills are listed from DSH; installation
  and configuration remain in the device's DSH configuration.
- **Sessions:** list, rename, pin, completion receipts and same-directory forks
  at completed native boundaries. Historical forks are source-validated.
- **Files:** paged directory browsing, source/Markdown/image/HTML/PDF/audio
  previews and existing exact-file authorization. Current Git diff is available;
  DSH does not supply cc-remote's per-turn file checkpoint archive.

Work, `/btw`, worktree/cwd migration, archive/delete and Codex token-budget or
account-quota controls are not DSH capabilities in this adapter and are hidden.
Agent Presets apply when creating a session; they are not changed on existing
sessions. DSH context usage reports native estimates and model capacity; the
Codex per-session context-window override does not apply to DSH.

## History and release boundaries

The ordinary `session/page` API needs a durable cursor. `session/follow` provides
one but also activates a cold Agent. `cc-remote.mjs` acquires an exact read lease
through public `sessionQuery`, then delegates paging to `typertGateway`. Its
`/api/cc-remote.snapshot` route uses DSH Connection's existing authentication.
It adds no server, credential store or model request. A missing bridge is an
explicit error; history never falls back to `follow`.

V3 durable sequence and native step identity determine ownership. Uncommitted
attempts retire provisional text; committed text replaces it. A late response
to a pre-steer question stays under that question, including after refresh.
Autonomous goal rounds get their own rows. Disconnects preserve the unfinished
state with a connection notice until the native terminal is known.

The Web, relay and Wrapper must all run **protocol v63**. Wrapper release bundles
include this bridge; DSH and its patch/configuration are independently managed.
Use [the repository release procedure](../../deploy/README.md) for deployment.

## Verification

Offline regressions (Node 24; no live model):

```sh
python -m pytest tests/test_dsh_client.py tests/test_dsh_stream.py tests/test_dsh_runtime.py
npm --prefix web run test:dsh
npm --prefix web run test:history-browser -- --grep DSH
```

The explicit native gate uses a **separate npm installation**, a temporary
DSH home/cwd/port, disabled production model adapters and one scripted adapter:

```sh
# In an empty disposable directory outside the repository:
npm install --ignore-scripts --save-exact @deepseek-ai/dsh@0.1.5-rc.2
# From this repository, with Node 24 on PATH:
python -m tests.dsh_native --installation /absolute/path/to/disposable-install
```

It exercises actual authentication, cold history, model/effort/permissions,
prompts, steering/cancel, questions/approvals, attachments, image reads, forks,
context, file browsing and goal commands. It also runs upstream's V0/V1/V2
physical codecs and migrations into V3: legacy `code` becomes `ptc`, original
fixture bytes stay unchanged, and unsupported records are refused. These are
isolated compatibility checks, not an upgrade of the operator's session store.
There is no reverse migration; keep original legacy files for the older DSH.

Run the complete repository gate before publishing or deploying this branch.

## Upstream contracts

- [DSH 0.1.5-rc.2](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.1.5-rc.2)
- [Session Controller](https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.1.5-rc.2/packages/api/session-controller)
- [Connection authentication](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.1.5-rc.2/packages/client/connection/src/browser-auth.ts)
- [Read-only session observations](https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.1.5-rc.2/packages/session-query/session-query)
- [Format migration](https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.1.5-rc.2/packages/session/session-format-v2-to-v3)
