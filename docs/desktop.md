# Legacy macOS Desktop compatibility

Web/PWA are the released graphical clients. The existing `cc.remote.desktop`
Electron prototype is a separately maintained macOS application; v5.3.7 Release
assets do not include a new Desktop installer. This document is about cc-remote
Desktop, not the official Codex App.

## Why an older Desktop can remain stuck

The legacy shell serves its bundled Web assets for the configured Relay origin.
That includes the page and `cc-remote-build.json`. When the Relay protocol advances,
the page reports a mismatch, but refreshing loads the same bundled page again.
A working browser alongside a Desktop message saying “waiting for server deployment”
can therefore indicate an outdated shell rather than an unfinished server update.

The compatibility repair retains the native browser integrations, request-origin
handling, permissions, connection settings and cookies. It changes only the known
asset handler to load the configured Relay's Web UI and manifest. Derived service
worker/cache storage for that origin is cleared at startup; login cookies and
local storage are retained. The repair does not disable the wire-version gate,
change Relay/Wrapper, restart an engine or move native sessions.

## Stage and verify

Use the current repository checkout on macOS. Supply the actual installed app
path and a new destination outside the app:

```bash
python3 deploy/desktop_remote_ui.py \
  --app '/absolute/path/to/cc-remote Desktop.app' \
  --stage '/absolute/path/to/staging/cc-remote Desktop.app'
```

The helper verifies the existing signature and bundle identity, recognizes the
complete legacy handler, copies the app, patches the staged copy and ad-hoc signs
and verifies it. It refuses unknown handlers, already repaired copies and existing
destinations. The original app and its private user data remain untouched. A local
ad-hoc signature is not a notarized distribution release.

1. Retain the helper's JSON record and both app copies. Quit the old Desktop.
2. Open the staged app and verify sign-in, the expected device/session list and
   a file preview against the configured Relay. Do not send a model turn solely
   to verify UI compatibility.
3. Quit the staged app. Recheck that the installed app still matches the recorded
   original hash and neither copy is running.
4. Move the original app to a retained rollback location, then move the staged
   app to the original location. Keep connection settings and user data in place.
5. Verify the installed signature, reopen it and check the configured Relay's
   build identity, device connectivity and history again. If activation fails,
   quit Desktop and restore the retained original app to its original path.

Never overwrite a running app in place. Retain the old app until acceptance and
normal repository retention review; do not delete private user data to fix a
version mismatch. `cc-remote update` still manages Relay, Web and Wrapper, while
Electron itself remains separately updated.

## README screenshots

The README gallery uses the actual v5.0.0 Web UI inside this Desktop shell,
connected to the loopback-only [showcase](../scripts/readme/README.md). Its data is
synthetic, with no private sessions, credentials, device names or project paths.
Screenshots illustrate UI capabilities, not results from a live model or shell.
