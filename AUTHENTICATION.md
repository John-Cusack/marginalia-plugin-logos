# Authentication

The Logos plugin authenticates by carrying a Faithlife browser session in a
cookie jar. There's no API-key path — Logos doesn't offer one for this product.

## Where the session lives

Everything is kept in the plugin data directory — the same directory the
engine hands the plugin at runtime:

```text
$RE_DATA_DIR/plugin-data/logos/        # or ~/.research-engine/plugin-data/logos/
  cookies.json                         # 0600 — the app session
  browser-profile/                     # 0700 — Chromium profile holding the Faithlife SSO session
```

`logos-login` and `logos-diagnose` resolve that directory by the engine's own
rule (`RE_DATA_DIR`, else `~/.research-engine`) without importing the engine,
so a login from a terminal writes exactly where the running server reads. If
you run the engine with `RE_DATA_DIR` set only in its `.env` file, export the
same value in the shell you run `logos-login` from.

Both files are credentials. They never belong in a repository, a backup you
share, or a support request.

## Upgrading from 0.1.x

Releases before 0.2.0 kept the session in `~/.logos-mcp/`. Nothing moves it
automatically; `logos.auth_status` and `logos.diagnose` tell you when a session
is waiting there. Move it once:

```sh
logos-login --migrate-data --dry-run   # inventory: what exists, sizes, what would be copied
logos-login --migrate-data             # copy, verify, then remove the originals
```

- Only `cookies.json` and `browser-profile/` move. Anything else in
  `~/.logos-mcp/` is left where it is.
- If the destination already holds a *different* `cookies.json` or profile,
  nothing is copied and the command exits 1. An identical one counts as
  migrated.
- The copies are made `0600` (cookies) and `0700` (directories); profile files
  keep their owner permissions with group/other access removed.
- The originals are removed only after the copies verify: the jar parses and
  the profile matches the source file for file (by size and SHA-256).
  `--keep-source` skips the removal; running the command again later finishes it.
- Stop the engine first. A profile held by a running Chromium is refused.
- `--from PATH` migrates from somewhere other than `~/.logos-mcp`.
- Cookie values are never printed.

Exit codes: 0 migrated, already migrated, nothing to migrate, or dry run;
1 refused (nothing changed); 3 copies did not verify (copies removed, source kept).

## First-time setup

```sh
# The auth extra installs Playwright; the browser binary is a separate,
# explicit step — installing the wheel never downloads a browser.
pip install 'marginalia-ai-plugin-logos[auth]'
python -m playwright install chromium

# Sign in. Opens a headed Chromium, waits for you to complete the flow,
# auto-detects when /api/app/me reports isAuthenticated:true, saves cookies.
logos-login
```

The login command does not ask you to press Enter — it polls the auth
endpoint and finishes on its own as soon as the server confirms your
session. If anything goes wrong in the browser (MFA prompt, captcha,
typo'd password), just keep working in the browser; the script will pick
it up the moment you succeed. Timeout is 10 minutes.

`logos-login --status` reports the stored session (and where it is) without
changing anything. `logos-login --refresh` renews silently from the SSO profile.

## Silent renewal runs a browser inside the engine

The Faithlife sign-in form is reCAPTCHA-protected, so the plugin never replays a
password. It keeps the long-lived SSO session in `browser-profile/` and, when the
app cookies lapse, the engine process launches **headless Chromium** against
that profile to have Faithlife re-issue them. That is why the plugin's manifest
declares `subprocess: true`, and why Playwright plus its Chromium must be
installed wherever the engine runs, not only where you ran `logos-login`.
Chromium's own traffic (the Logos app, the Faithlife OAuth pages and whatever
those pages load) is a browser's, not a request made through the engine's
scoped HTTP client.

## Health check

```sh
logos-diagnose
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
  "file":  { "path": "/home/you/.research-engine/plugin-data/logos/cookies.json",
             "exists": true, "age_s": 42.1, "size_bytes": 4084 },
  "profile": { "path": "/home/you/.research-engine/plugin-data/logos/browser-profile",
               "seeded": true },
  "legacy_session": null,
  "jar":   { "count": 22, "has_auth2": true, "auth_cookie_value_len": 307 },
  "cache_age_s": 41.9,
  "live_check": { "authenticated": true, "alias": "John", "email": "..." }
}
```

When `live_check.authenticated` is `false`, the other fields localize the
failure:

| Symptom | Most likely cause | Fix |
|---|---|---|
| `file.exists: false` and `legacy_session` set | Upgraded from 0.1.x; the session is still in `~/.logos-mcp` | Run `logos-login --migrate-data`. |
| `file.exists: false` | Never logged in (or `logout()` was called), or `RE_DATA_DIR` differs between the engine and your shell | Compare `file.path` with where you logged in; run `logos-login`. |
| `jar.count: 0` | File present but couldn't be parsed | Re-run `logos-login` to overwrite the file at `file.path`. |
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
