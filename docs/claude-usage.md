# Claude account quota

The quota meter reads on session focus/reconnect, when opened or refreshed, and
once per minute while the Claude page is visible. Returning to the page resumes
these periodic reads. This does not send a prompt, resume an engine, modify history,
interrupt work, or consume model tokens. Native `RateLimitEvent` updates remain
enabled. The Wrapper coalesces reads per account with a 15-second minimum
interval, including failures, across sessions and browser clients.

The pinned Agent SDK exposes quota events but no account-usage pull method.
Active reads therefore use an optional **operator-owned usage helper**. The
provider/login tool retains its credentials and performs its authenticated
read; cc-remote never reads OAuth, keychain, API key or gateway token files.
No helper or authentication data comes from the browser. A helper must be a
read-only query, not `claude -p`, `/usage` submitted as a chat prompt, or a tool
that logs in, redeems credits or refreshes model conversations.

Place a private, mode-0600 `claude-usage-helpers.json` in `CC_REMOTE_STATE_DIR`
(normally `~/.cc-remote`). It is external configuration and survives upgrades.
Use absolute paths and bind every entry to its actual native account root:

```json
{
  "version": 1,
  "profiles": {
    "primary": {
      "config_dir": "/absolute/native/account/root",
      "command": ["/absolute/path/to/provider-usage-helper"]
    }
  }
}
```

The executable runs without a shell, from the service user's home. It receives
one JSON object on stdin: `version`, `profile_id` and `config_dir`. Only basic
OS environment fields are inherited; model credentials and account selectors
are not. The helper must verify that this account is the one its provider
connection represents. Keep credentials outside arguments and output. Never
reuse another profile's gateway token merely because the URLs match.

On success, stdout contains one JSON object with the read-only OAuth usage
shape below; exit nonzero on failure. The Wrapper limits execution to 15 seconds
and stdout to 64 KiB. Stderr and raw errors are never shown or logged.

```json
{
  "five_hour": {"utilization": 37, "resets_at": "2026-10-01T12:00:00Z"},
  "seven_day": {"utilization": 12.5, "resets_at": "2026-10-05T12:00:00Z"},
  "seven_day_opus": null,
  "seven_day_sonnet": null
}
```

These `utilization` values are consumed **percentages from 0 to 100**; SDK
events instead use fractions from 0 to 1. Reset times must include a timezone;
null means unknown. A null window clears its cached value. Unknown fields,
account details and paid-credit balances are discarded. Newer native events
win over older in-flight HTTP results. Invalid results do not replace valid
observations. Cached windows expire individually at their reset time.

A missing helper keeps native-event synchronization and shows that active
reads are not configured; this does not mean the provider lacks a usage API.
A failed read keeps still-valid observations with a
refresh-failure notice; absent data is not reported as exhausted quota.
The existing status wire shape is reused; Codex's app-server status, coupons
and account authentication paths are unchanged.

For a private gateway, the provider helper can GET its existing
`/api/oauth/usage` endpoint using the gateway's own authentication and release
requirements. Verify the actual endpoint, account binding and response before
enabling the helper. cc-remote does not provision or change that gateway.
