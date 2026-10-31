# cc-remote HarmonyOS client (preview)

This is a HarmonyOS **HAP** application using ArkTS/ArkUI and the system's ArkWeb
component. It opens directly from the launcher and loads the existing cc-remote
HTTPS UI. It does not use Android, 卓易通, or an external browser for the chat.
The conversation UI stays web-based; it is not a rewrite of the UI in ArkUI.

## Current scope

- HTTPS server setup, followed by the normal cc-remote cookie login.
- No additional address bar or native-only menu entries in chat.
  Server selection and app information remain in **客户端设置** on the login page
  and the native load-error screen.
- External HTTPS links open in the system browser, including user-triggered
  new windows. The chat remains mounted and retains its draft when returning.
  Same-origin new windows retain **返回聊天** and the app's login cookies.
- System file selection through ArkWeb's default file-upload handling.
- System-bar and keyboard avoidance; the page retains its existing sidebar
  gestures, rounding and fading. No additional native horizontal recognizer or
  Android-specific blur override is installed.
- App background/foreground never reloads chat or submits a model message.

Camera/microphone capture, download management, non-HTTPS external
app links and browser-control MCP are outside this preview. Native completion
notifications have a separate optional Push Kit integration described below. Persistent
login remains subject to the Relay cookie's normal expiry. No cookie, model
credential, session payload or device-specific server address is stored in this
repository. Only the user-entered server origin is saved in native preferences.

## Prepare and build on macOS

Install the official **Mac ARM** edition of
[DevEco Studio](https://developer.huawei.com/consumer/cn/download/deveco-studio).
The project uses the Stage model, compatible API 14+, and APIs available by
API 20. Compilation uses DevEco's bundled HarmonyOS SDK by default; the initial
build was checked with DevEco Studio 26.0.0 and its API 26 SDK.

From the repository root:

```bash
python3 harmony/tools/project.py init
python3 harmony/tools/project.py check
python3 harmony/tools/project.py build
```

For a nonstandard installation, add `--studio /path/to/DevEco-Studio.app`.
Open **the `harmony/` directory** in DevEco Studio. The `init` command creates the
two local `build-profile.json5` files from tracked templates. These local files,
`local.properties`, IDE files and signing certificates are gitignored. Existing
local profiles are never overwritten; do not commit automatic-signing fields or
certificate passwords to the templates. No server deployment is needed.

## Install on your own phone

This app can be tested without an application-market listing, but a real device
still requires a valid debug signature. In DevEco Studio:

1. Sign in to your Huawei developer account and complete its required account
   verification if prompted.
2. Enable developer options and USB debugging on the phone, connect it to the
   Mac, and accept the device's debugging prompt.
3. Open **File → Project Structure → Signing Configs**. Use automatic debug
   signing; association with a registered market application is not required
   for this basic private trial. Ensure the `default` product uses the generated
   signing configuration. If a command-line build produces only an unsigned HAP,
   check `app.products[].signingConfig` in the local `build-profile.json5`.
4. Select the connected phone and Run. Enter your HTTPS Relay origin and log in.

The debug profile is bound to allowed devices and has an expiry. Check that
expiry in the IDE; renew/re-sign as required. An unsigned HAP is a build artifact,
not a file that can be tapped to install on a normal retail phone. Keep the same
bundle name and signing identity for upgrades, incrementing `versionCode`.

## Updates and daily use

Every new cc-remote GitHub Release includes an unsigned Harmony HAP, along with
`MOBILE_BUILD_INFO.json` and `MOBILE_SHA256SUMS`. The filename follows the cc-remote
release; the app's actual native version and original build source remain in the
metadata. This does not make the HAP directly installable without your own signing
profile.

The release workflow reuses the verified build pinned in
[`deploy/harmony-release.json`](../deploy/harmony-release.json). It checks the
downloaded bytes, archive integrity, app identity and native source equality before
publishing. A missing or invalid HAP fails the release instead of silently omitting
it. Documentation and test-only changes may reuse the same app; changes to native
code or build inputs require a new verified unsigned build and an updated pin.
Preserve the original builder/SDK/source information when updating the pin. The
locally compiled HAP and its mobile metadata are separate from the Actions-attested
Relay/Wrapper bundles; republishing does not claim a new native build or device test.

The installed app runs independently of DevEco Studio and the USB connection.
After installation, exit the IDE and disconnect the phone. If the local HDC
server remains running, stop it with
`<DevEco-Studio.app>/Contents/sdk/default/openharmony/toolchains/hdc kill`.
Keep the SDK and private signing configuration for the next native update.

Most chat features come from the hosted Web UI. After a normal cc-remote server
update, close the app completely and reopen it to load the updated UI; a new HAP
is not needed. Changes to the ArkUI/ArkWeb shell or its packaged adapter require a
new signed HAP and an in-place installation. Preserve the bundle and signing
identity, increment `versionCode`, and install over the existing app instead of
uninstalling it so its local preferences and browser data can be retained.
Cookie expiry still follows the Relay's login policy. This preview does not
include an in-app native updater, and its debug profile must be renewed when it
expires.

The Push Kit preview uses `io.github.ccremote.client`. The first unassociated
preview used `io.github.ccremote.harmony`, which Huawei's application registry
rejects because `harmony` is reserved. Moving from that preview is a separate
installation, not an in-place update: retain the old app until the new one is
verified and sign in again. Server-side conversations stay on the Relay/Wrapper;
local drafts, preferences and login cookies do not automatically migrate.

Official references:

- [Automatic signing](https://developer.huawei.com/consumer/cn/doc/doccenter-deveco-studio/ide-signing-auto)
- [Signing requirements](https://developer.huawei.com/consumer/cn/develop-novice-guide/)
- [ArkWeb file upload](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/web-file-upload)
- [ArkWeb new windows](https://developer.huawei.com/consumer/cn/doc/doccenter-capabilities/web-open-in-new-window)
- [System application redirection](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/deep-linking-startup)

## Verification

With Node 24 selected and the existing `web/` development dependencies installed:

```bash
node --test harmony/tests/url-policy.test.mjs harmony/tests/external-browser.test.mjs harmony/tests/push-route.test.mjs harmony/tests/push-bridge.test.mjs
node web/node_modules/@playwright/test/cli.js test -c harmony/playwright.config.mjs
```

The policy tests execute the production TypeScript with a WHATWG URL substitute
for ArkTS URL. Browser checks use the real Web settings menu and sidebar fixture.
Neither substitutes for ArkTS compilation or a real-device ArkWeb test.

The initial preview was compiled and signed with DevEco Studio 26.0.0, installed
on a physical HarmonyOS 7 phone, and checked at first launch. The operator also
reported smooth sidebar gestures. This is a usability check, not a measured
frame-rate result or completion of every acceptance item below. The URL-policy
suite (5 cases) and browser adapter suite (4 cases) passed.

Before calling a package accepted, compile it with DevEco, install a signed build
on the target phone, and check:

- Cold open, login, app background/foreground, login persistence and reconnect.
- Open/close the sidebar slowly and with a flick, both idle and while streaming;
  compare the same session with Huawei Browser. Do not claim a measured frame
  rate without a device capture.
- Keyboard resize, attachment selection/cancel/reselect, and file previews.
- Open external HTTPS links (regular navigation, target-blank and a user-created
  blank window assigned a destination later) in the system browser; return to the
  unchanged chat/draft. Automatic redirects must not launch another app. Check a
  same-origin target-blank preview still uses the app's login and **返回聊天**;
  cancel a popup while it is attaching. Check browser launch failures show a
  retryable message without replacing the chat.
- Client settings, cancelling server change, offline recovery and server change.
- TLS errors remain rejected, and external/iframe pages cannot open client
  settings or access privileged native methods.

There are no exposed JavaScript proxy objects, credentials in URLs, TLS bypass,
frame-origin relaxations, or engine/daemon changes. Settings opens only after an
exact-origin, top-frame and user-gesture check. A narrow Push Kit message port is
installed only in the configured chat document; external pages/preview frames do
not receive it. Notification URLs contain navigation hints only, never login
credentials, and can only select a session after normal Web authorization.


## Optional HarmonyOS completion notifications

This channel uses **Huawei Push Kit**, independently of the browser's Web Push
and VAPID configuration. It does not require a running WebSocket on a locked
phone. Android/Web/PWA notification behavior is unchanged. The default is Off;
there is no local-notification or polling fallback pretending to be background
push. The settings page offers the existing three modes:

- **关闭**: invalidate the native token and remove its Relay subscription.
- **通用提醒**: send the completion/failure/interruption outcome only; no machine
  ID, session ID, title, prompt or transcript is sent to Huawei.
- **显示会话名称**: explicitly allow a bounded title and exact device/session
  route. Clicking a notification opens the chat without reloading an existing
  document or losing its draft. A notification for a different configured Relay
  is ignored. Normal device/session authorization still applies.

### Huawei and Relay setup

1. Register/associate the application in your Huawei developer project, enable
   **Push Kit**, and obtain the corresponding Push capability in its signing
   profile. Re-sign and install an in-place update. An unassociated debug profile
   that runs the basic client is not sufficient. Preserve the installed bundle
   and signing identity; do not uninstall or silently change the bundle to solve
   a registration error. No application-market listing is needed for development
   testing, but Huawei's current entitlement and classification rules apply.
2. In Huawei API Console, create a service account for the project's Push API
   and download its JSON key. Keep it **on the Relay only**, outside this repo and
   release directories, with restrictive permissions readable by the service.
   The JSON contains `project_id`, `key_id`, `sub_account` and an RSA PEM
   `private_key`. Nothing from this file belongs in the HAP or Web bundle.
3. Configure the Relay's external environment:

   ```dotenv
   HARMONY_PUSH_SERVICE_ACCOUNT=/absolute/private/path/service-account.json
   HARMONY_PUSH_CATEGORY=MARKETING
   HARMONY_PUSH_TEST_MESSAGE=0
   ```

   `WORK` is supported only if Huawei has granted the application that
   classification. Do not assign an unauthorized category to bypass delivery
   quotas. `HARMONY_PUSH_TEST_MESSAGE=1` is an explicit development option with
   Huawei's test quotas; turn it off for production. The provider's successful
   API response means accepted, not guaranteed delivery to a device.
4. Update/restart the Relay through the normal deployment workflow, update the
   Web assets and native package, then choose a notification mode in **⋮ → 通知**.
   Allow the system notification permission. Missing Relay configuration or an
   unsigned Push capability produces an actionable error instead of saving a
   falsely enabled setting.

`/api/push/harmony/config`, `/subscribe`, and `/unsubscribe` use the existing
Relay login cookie. Mutations require the existing allowed Origin and selected
machine authorization. Tokens bind to the current login and selected machine,
are replaced on rebinding, expire with the login, and are removed on logout or
provider rejection. They are stored in a separate table in `PUSH_DB_PATH`, not in
model configuration. On server change, the shell invalidates the old provider
token before clearing cookies; if invalidation fails, it retains the old server.
Generic push cannot offer exact session navigation without disclosing the route.

This integration intentionally does not add a PushExtensionAbility/background
worker, a persistent foreground service, or notification-triggered model calls.
Foreground notices are suppressed by Push Kit's `foregroundShow: false` setting.
Token state is reconciled on reload, app foreground and provider token updates.
Native key/API failures are isolated from the Web Push dispatcher. Neither the
wire protocol nor Wrapper/Claude/Codex lifecycle changes.

### Verification and remaining device acceptance

Offline tests cover PS256 signing, HTTP/provider result codes, auth/Origin/device
scope, logout/expiry, stale rebind protection, generic-mode privacy, native port
isolation, disabled configurations, URL routing and the unchanged VAPID channel.
The native project must also compile with DevEco. These checks **do not prove
Huawei delivery**: with Push Kit enabled and the matching signed package, verify
foreground/background/locked/process-terminated delivery, permission denial,
server switch, logout, token refresh and cold/warm notification navigation on a
real phone before calling push enabled for that installation.

Official references:

- [Push Kit setup and signing](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/push-config-setting)
- [Service-account JWT](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/push-jwt-token)
- [Push request fields and limits](https://developer.huawei.com/consumer/en/doc/harmonyos-references/push-scenariozed-api-request-param)
- [ArkWeb message ports](https://developer.huawei.com/consumer/cn/doc/HarmonyOS-Guides/web-app-page-data-channel)
