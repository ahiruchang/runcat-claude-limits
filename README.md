# runcat-claude-limits

A [RunCat Neo](https://github.com/runcat-dev/RunCatNeo) **Custom Metrics** source
that shows your Claude Code rate-limit lanes — the rolling **5-hour** window, the
**weekly (all-models)** window, and the **model-scoped weekly** window (e.g.
`Fable`) — each with its reset time, on a menu-bar card.

```
Claude Code
  5h:    41%  ⟳7/20 16:50
  7d:    16%  ⟳7/26 11:00
  Fable: 28%  ⟳7/26 11:00
```

Lanes are discovered **dynamically** from the usage API's `limits[]` array, so no
model name is hardcoded: the top-model weekly lane follows whatever it is called,
and extra scoped lanes (Opus / Sonnet / …) appear automatically if your account
has them.

## How it works

- When run as a Claude Code `statusLine` command, the script reads the
  `rate_limits` object from stdin (`five_hour` / `seven_day`) — freshest, no API
  call — and uses those for the 5h / 7d lanes.
- Every lane, **including the model-scoped one** (which is *not* in the stdin
  payload), is read from the undocumented OAuth usage endpoint
  `https://api.anthropic.com/api/oauth/usage`, authenticated with Claude Code's
  **own OAuth token** (macOS Keychain item `Claude Code-credentials`, or
  `~/.claude/.credentials.json`). No new credential is stored; the token is read
  at runtime.
- That endpoint is aggressively rate-limited, so its result is **cached** and
  fetched at most once per `RUNCAT_REFRESH_SEC` (default 300 s). On any failure
  the previous cached values are kept, so the card degrades gracefully.
- The script writes a RunCat Custom Metrics JSON snapshot and prints a compact
  line to stdout for the terminal status line.

## Requirements

- macOS with [RunCat Neo](https://apps.apple.com/app/runcat-neo/id6748945664) (App Store)
- [Claude Code](https://claude.com/claude-code) signed in to a Claude
  subscription (Pro / Max) — the rate-limit lanes only exist for subscribers
- `python3` (standard library only)

## Setup

1. Copy the script and make it executable:
   ```bash
   cp runcat-claude-limits.py ~/.claude/runcat-claude-limits.py
   chmod +x ~/.claude/runcat-claude-limits.py
   ```
2. Register it as your `statusLine` in `~/.claude/settings.json` (use an absolute
   path; replace `YOU` with your home):
   ```json
   {
     "statusLine": {
       "type": "command",
       "command": "/Users/YOU/.claude/runcat-claude-limits.py"
     }
   }
   ```
   > `settings.json` allows only one `statusLine.command`. If you already have one,
   > merge the two scripts yourself.
3. In RunCat Neo, open **Settings → Metrics → Custom Metrics**, click
   **Add JSON Source**, and choose `~/.claude/runcat-claude-limits.json`.
4. Use Claude Code — the card updates each turn.

If the card only shows 5h / 7d (no scoped lane) and the cache reports
`"err": "...401..."`, your CLI token is stale: run `claude auth login`.

## Configuration (environment variables)

| Variable | Default | Purpose |
|----------|---------|---------|
| `RUNCAT_OUT_FILE` | `~/.claude/runcat-claude-limits.json` | Output path RunCat reads |
| `RUNCAT_REFRESH_SEC` | `300` | Min seconds between OAuth API calls (≥180 recommended) |
| `RUNCAT_USER_AGENT` | `claude-cli/<version> (external, cli)` | Override the API User-Agent |
| `RUNCAT_FABLE_OFF` | unset | `1` = skip the API call (stdin 5h/7d only) |
| `RUNCAT_DEBUG` | unset | `1` = dump the raw OAuth response to `…-oauth-debug.json` |

## Caveats

- **Undocumented endpoint.** `/api/oauth/usage` is not a public Anthropic API and
  may change or break without notice. The 5h / 7d lanes keep working from stdin
  even if it does.
- **Rate limits.** The endpoint 429s aggressively; keep `RUNCAT_REFRESH_SEC` at
  180 s or more. The default User-Agent mimics the CLI to avoid an even stricter
  bucket.
- **Token freshness.** The OAuth token is refreshed by Claude Code while you use
  the CLI. If the CLI sits idle long enough for the token to expire, the
  scoped-lane value freezes until the next CLI use (5h / 7d still update from
  stdin).
- **"Last updated".** RunCat renders `lastUpdatedDate` as a relative time and does
  not tick it live, so it only refreshes when the card re-reads the file.

## Credits & license

Derived from RunCat Neo's official
[`docs/samples/claude-code`](https://github.com/runcat-dev/RunCatNeo/tree/main/docs/samples/claude-code)
sample (Apache-2.0), extended with the OAuth usage endpoint, dynamic
model-scoped lanes, and reset-time display. The `/api/oauth/usage` approach was
figured out by the Claude Code community.
