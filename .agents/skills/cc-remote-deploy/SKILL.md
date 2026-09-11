---
name: cc-remote-deploy
description: Deploy, upgrade, verify, or recover cc-remote using this repository's release workflow. Verify shared Codex CLI control and optionally attach the macOS or Linux Codex App to the same account's daemon.
---

# Deploy cc-remote

This is the portable repository skill, not a machine-specific deployment recipe.
Resolve the cc-remote checkout first and run commands from its root. The links
below resolve within that checkout; keep this skill with it rather than copying
the skill alone into a global skills directory. When invoked outside the
checkout, ask for its location.

## Deployment source of truth

Before deployment actions, read the complete
[`deploy/README.md`](../../../deploy/README.md) automation contract and the
selected installation path in [`docs/installation.md`](../../../docs/installation.md)
or [`docs/installation_en.md`](../../../docs/installation_en.md). Follow the local gate in
[`AGENTS.md`](../../../AGENTS.md). Do not maintain a second set of activation,
rollback or protocol-version instructions here.

Prefer one tested source snapshot for current features. Use a published Release
when the user selects that version and its contents meet the requested scope;
do not assume the latest published tag includes the current branch's features.

Use the operator's actual inventory and existing service ownership. Freeze one
tested snapshot for all protocol tiers, preserve private configuration/state,
and use the repository's immutable activation transactions. Lost connectivity
means an unknown result: inspect the original operation before retrying. Do not
overwrite live directories or restart the controlling Wrapper from itself.

## Codex CLI sharing is an acceptance check

For every enabled Codex **Code** account, follow
[shared control plane acceptance](../../../deploy/README.md#codex-code-shared-control-plane-acceptance).
Check the daily CLI and Wrapper's actual connection to the **same official
daemon**, not just matching session files or a healthy Web UI. Accounts keep
separate `CODEX_HOME` boundaries; Work's private control plane is not changed.

Do not assume npm versus standalone decides sharing. Verify ordinary
`codex resume <session-id>` routing; explicit `--remote` success is not proof of
automatic discovery. Never kill a live CLI, force takeover, modify shell aliases,
or start a model turn without explicit authorization. Report unverified sharing or
stdio fallback separately from Relay/Web health.

## Offer Codex App attachment after deployment

After core health and CLI sharing checks, follow
[the optional App checkpoint](../../../deploy/README.md#optional-codex-app-attachment).
Inspect only in-scope Wrapper desktops. If a compatible macOS or Linux Codex App is
installed and not already verified as shared, ask whether the user wants it to
join the chosen account's CLI/cc-remote daemon. Do not infer consent from the
deployment request. A decline or no answer leaves the App unchanged and does
not block the core deployment. An explicit attachment request or an existing
approved setup is already consent; do not ask again for the same account/setup.

After consent, read the matching complete runbook:

- macOS: [shared Desktop launcher](../../../docs/codex-desktop-launcher.md),
  using the repository's macOS helper.
- Linux: [App, CLI and Wrapper sharing](../../../docs/codex-desktop-linux.md),
  using a separate account-scoped launcher and the verified direct Unix socket
  transport. The Linux package may be named ChatGPT; select its Codex mode.
  Do not run the macOS helper, install a desktop on a headless host, or assume
  an App installed over SSH has a logged-in graphical session.

Preserve account boundaries and existing launchers. Do not patch the official
App, change the default account, export global launch variables, or stop active
App/CLI/daemon processes. Verify ordinary CLI resume, the Wrapper's profile route,
and the App's live connection against one actual daemon identity. App login and
history visibility alone do not establish three-client sharing.

Sharing the conversation does not authorize extra App-control tools. Only if the
user also wants those tools, read [the optional MCP guide](../../../docs/codex-app-tools.md)
and preserve the installed official tool approval policy and signature checks.
Check the installed native plugin first; the custom adapter is currently macOS-only.
The tools operate the desktop App, not cc-remote's Web sidebar.

## Handoff

Report core release/health separately from each account's CLI route and optional
App state: verified, declined, pending consent, unavailable, or awaiting an
operator-controlled reopen. Distinguish transport checks from a token-spending
live-message test. Give the selected shared launcher entry if installed; never
claim three-way sharing based only on files, an installed icon or a socket path.
