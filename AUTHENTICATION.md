# Authentication

The Logos plugin authenticates by carrying a Faithlife browser session in a
cookie jar at `~/.logos-mcp/cookies.json`. There's no API-key path — Logos
doesn't offer one for this product.

## First-time setup

```sh
# Install the plugin with the auth extra (pulls in Playwright + Chromium).
uv sync --extra auth
playwright install chromium

# Sign in. Opens a headed Chromium, waits for you to complete the flow,
# auto-detects when /api/app/me reports isAuthenticated:true, saves cookies.
uv run logos-login
```

The login command does not ask you to press Enter — it polls the auth
endpoint and finishes on its own as soon as the server confirms your
session. If anything goes wrong in the browser (MFA prompt, captcha,
typo'd password), just keep working in the browser; the script will pick
it up the moment you succeed. Timeout is 10 minutes.

## Health check

```sh
uv run logos-diagnose
```

Prints a single JSON document covering every auth layer. Exit code is 0
when `live_check.authenticated` is true, 2 otherwise — useful in CI.

The same diagnostic is exposed as an MCP tool: call
`mcp__research-engine__logos_diagnose` (no arguments) to get the same
information from inside an agent session.

## Reading the diagnostic

A healthy response looks like:

```json
{
  "file":  { "exists": true, "age_s": 42.1, "size_bytes": 4084 },
  "jar":   { "count": 22, "has_auth2": true, "auth_cookie_value_len": 307 },
  "cache_age_s": 41.9,
  "live_check": { "authenticated": true, "alias": "John", "email": "..." }
}
```

When `live_check.authenticated` is `false`, the other fields localize the
failure:

| Symptom | Most likely cause | Fix |
|---|---|---|
| `file.exists: false` | Never logged in (or `logout()` was called) | Run `uv run logos-login`. |
| `jar.count: 0` | File present but couldn't be parsed | Inspect `~/.logos-mcp/cookies.json`; re-run `logos-login` to overwrite. |
| `jar.has_auth2: false` and `has_auth_legacy: true` | Saved jar is from before the `auth2` rollout | Run `logos-login` to refresh. |
| `jar.has_auth2: true` but `auth_cookie_value_len < 200` | Captured an anonymous session cookie (saved before sign-in completed) | Run `logos-login` again; this version polls the live endpoint so it can't repeat the mistake. |
| All looks healthy, `live_check.status_code: 401` | Server-side session was revoked or expired | Run `logos-login` to re-authenticate. |
| `live_check.error: "..."` | Network or DNS failure reaching `app.logos.com` | Check your connection; retry. |

## When auth fails inside a running MCP session

The in-memory cookie cache is mtime-aware. If you re-run `logos-login`
while the MCP server is up, the next request transparently picks up the
new cookies — **no server restart needed**. Call
`mcp__research-engine__logos_diagnose` to confirm `live_check.authenticated`
flipped to `true`.

If you ever need to fully log out (clear cache *and* delete the file),
import `logos.auth.manager.logout()` from Python. The CLI doesn't expose
this on purpose — accidentally deleting the cookie file used to be the
most common foot-gun in this flow.

## Cookie-name rotations

Faithlife rotated their session cookie name from `auth` to `auth2` in
2026. The plugin accepts either via `LogosCookieJar.auth_cookie`. If they
rotate again, the polling-based `logos-login` will still capture the new
session correctly (it doesn't look at cookie names — it asks the server
whether the session is valid). Only the `auth_cookie` presence check in
`logos/lib/types.py` would need a one-line update.
