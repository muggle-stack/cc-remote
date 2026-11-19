# Claude account quota

The quota meter reads the Wrapper's account cache on session focus/reconnect,
when opened or refreshed, and once per minute while the Claude page is visible.
Native `RateLimitEvent` updates remain enabled. Active helper queries are **off
by default**, even if a helper is already configured or the cache is empty.
The subscription badge can use Claude Code's saved account display name for the
selected profile while active queries are disabled. Only the display name is
sent to the browser. It reflects the last account information saved by Claude
Code, and a display name from a successful helper read takes precedence.
For an opted-in account, these same reads also refresh through its helper, with
a 15-second minimum interval, including failures, across sessions and clients.
Neither path sends a prompt, resumes an engine, modifies history, interrupts
work, or consumes model tokens. There is no SDK query or model-request fallback.

The pinned Agent SDK exposes quota events but no account-usage pull method.
Active reads therefore use an optional **operator-owned usage helper**. The
provider/login tool retains its credentials and performs its authenticated
read; cc-remote never reads OAuth, keychain, API key or gateway token files.
No helper or authentication data comes from the browser. A helper must be a
read-only query, not `claude -p`, `/usage` submitted as a chat prompt, or a tool
that logs in, redeems credits or refreshes model conversations.

Enable `usage_query_enabled: true` only for the intended account in the local
Claude profile registry (see [account configuration](configuration_en.md#multiple-accounts)):

```json
{
  "personal": {"label": "Personal", "config_dir": "/home/youruser/.claude", "default": true},
  "custom": {"label": "Custom", "config_dir": "/home/youruser/.claude-custom", "usage_query_enabled": true}
}
```

The field is a strict boolean; absent or false means native synchronization
only. Existing helper users must explicitly opt in after upgrading. A legacy
single account can use an explicit one-entry registry with its original native
root; this keeps its session IDs. No account name or provider is special.
The account configuration takes effect when Wrapper next starts. This setting
and helper command stay local, never browser-supplied.

Place a private, mode-0600 `claude-usage-helpers.json` in `CC_REMOTE_STATE_DIR`
(normally `~/.cc-remote`). It is external configuration and survives upgrades.
Use absolute paths and bind every entry to its actual native account root:

```json
{
  "version": 1,
  "profiles": {
    "custom": {
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
  "plan_display_name": "Ultra",
  "five_hour": {"utilization": 37, "resets_at": "2026-10-01T12:00:00Z"},
  "seven_day": {"utilization": 12.5, "resets_at": "2026-10-05T12:00:00Z"},
  "monthly": {"utilization": 16, "resets_at": "2026-11-01T00:00:00Z"},
  "seven_day_opus": null,
  "seven_day_sonnet": null,
  "seven_day_fable": {"utilization": 20, "resets_at": "2026-10-05T12:00:00Z"}
}
```

These `utilization` values are consumed **percentages from 0 to 100**; SDK
events instead use fractions from 0 to 1. Reset times must include a timezone;
null means unknown. A null window clears its cached value. Unknown fields,
account details and paid-credit balances are discarded. Newer native events
win over older in-flight HTTP results. Invalid results do not replace valid
observations. Cached windows expire individually at their reset time.

The optional `plan_display_name` comes from the provider's current account
response for this same login. The Wrapper accepts known display names and
keeps them in memory for successful helper reads. A helper that only returns
usage can omit this field. Fable appears with a reported percentage and reset
time, either as `seven_day_fable` or in the provider's `limits` array as a
`weekly_scoped` entry with `scope.model.display_name` set to `Fable`.

`monthly` is optional and uses the same percentage/reset fields. It represents
the provider's actual subscription cycle, never a guessed 30-day duration. A
monthly observation without a reset expires after 15 minutes for freshness.
The UI shows only present windows, in 5-hour / weekly / monthly order: 5-hour
plus monthly, monthly only, and all three are supported without empty rows.
Use `seven_day: null` when a snapshot explicitly has no weekly quota, and
likewise null any other unsupported window to clear an older cached value.
Missing fields preserve existing observations, so sparse native events cannot
erase a helper's monthly quota. The context ring remains a separate control.

A disabled query keeps native-event synchronization and reports the disabled
setting without an error. An enabled account without a helper shows that its
query source needs configuring; it does not try an inferred URL or SDK method.
A failed read keeps still-valid observations with a
refresh-failure notice; absent data is not reported as exhausted quota.
Protocol v83 adds optional `monthly` to account quota snapshots/events and
`quota_query_state` to status reports. All control-link tiers must upgrade
together. Codex's app-server status, coupons and account authentication paths
are unchanged.

For a private gateway, the provider helper can GET its existing
`/api/oauth/usage` endpoint using the gateway's own authentication and release
requirements. Verify the actual endpoint, account binding and response before
enabling the helper. cc-remote does not provision or change that gateway.
