#!/usr/bin/env python3
#
# Copyright 2026 Hiroki Nakano
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
runcat-claude-limits — RunCat Neo Custom Metrics source for Claude Code limits.

Shows every rate-limit lane the account exposes, each with its reset time, on a
RunCat Neo card. Lanes are discovered DYNAMICALLY from the usage API's `limits[]`
array — nothing is hardcoded to a specific model name, so the top-model weekly
lane follows whatever it is called (e.g. "Fable"), and any additional scoped
lanes (Opus / Sonnet / …) appear automatically if the account has them.

Typical lanes:
  * 5h      — rolling 5-hour session limit          (kind: session)
  * 7d      — weekly, all models                     (kind: weekly_all)
  * <model> — weekly, model-scoped (e.g. Fable)      (kind: weekly_scoped)

Data sources:
  * When run as a Claude Code statusLine, stdin carries payload.rate_limits with
    five_hour / seven_day (used_percentage + resets_at epoch). These overlay the
    session / weekly_all lanes when present (freshest, no API call).
  * All lanes — including model-scoped ones, which are NOT in the stdin payload —
    come from the undocumented OAuth usage endpoint
    (https://api.anthropic.com/api/oauth/usage), authenticated with Claude Code's
    own OAuth token (macOS keychain item "Claude Code-credentials", or
    ~/.claude/.credentials.json).
  * The OAuth endpoint is aggressively rate-limited, so its result is CACHED and
    called at most once per RUNCAT_REFRESH_SEC (default 300s, >=180s recommended).
    On any failure (401/429/timeout) the previous cached values are kept.

If the OAuth call returns 401 the CLI token is stale — run `claude auth login`
to refresh it. The endpoint is undocumented and may change or break without
notice; the 5h/7d lanes still work from stdin regardless.

Derived from RunCat Neo's official docs/samples/claude-code, extended with
dynamic model-scoped lanes and reset-time display.

Env overrides:
  RUNCAT_OUT_FILE      output path (default ~/.claude/runcat-claude-limits.json)
  RUNCAT_REFRESH_SEC   min seconds between OAuth API calls (default 300)
  RUNCAT_USER_AGENT    override the User-Agent for the API call
  RUNCAT_FABLE_OFF=1   skip the OAuth call entirely (stdin 5h/7d only)
  RUNCAT_DEBUG=1       dump the raw OAuth response to <base>-oauth-debug.json
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "runcat-claude-limits"
CLAUDE_DIR = Path.home() / ".claude"
OUT = Path(os.environ.get("RUNCAT_OUT_FILE", str(CLAUDE_DIR / f"{BASE}.json")))
CACHE = CLAUDE_DIR / f"{BASE}-cache.json"
LOCK = CLAUDE_DIR / f".{BASE}.lock"
DEBUG = CLAUDE_DIR / f"{BASE}-oauth-debug.json"
REFRESH = int(os.environ.get("RUNCAT_REFRESH_SEC", "300"))
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
FABLE_OFF = os.environ.get("RUNCAT_FABLE_OFF") == "1"
DEBUG_ON = os.environ.get("RUNCAT_DEBUG") == "1"

# Friendly labels for the non-scoped, well-known lane kinds. Scoped lanes are
# labelled dynamically from scope.model.display_name.
KIND_LABELS = {"session": "5h", "weekly_all": "7d"}


# ---------- stdin payload (Claude Code statusLine) ----------
try:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        payload = {}
except Exception:
    payload = {}

rl = payload.get("rate_limits") or {}


def stdin_lane(key):
    d = rl.get(key) or {}
    return d.get("used_percentage"), d.get("resets_at")  # resets_at = unix epoch seconds


sin_five_pct, sin_five_reset = stdin_lane("five_hour")
sin_seven_pct, sin_seven_reset = stdin_lane("seven_day")


# ---------- User-Agent (must look like the Claude Code CLI or the endpoint drops
#            the request into an aggressively rate-limited bucket) ----------
def claude_version():
    v = payload.get("version")
    if isinstance(v, str) and v.strip():
        return v.strip()  # statusLine payload carries the CLI version — no subprocess
    try:
        exe = shutil.which("claude")
        if exe:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5).stdout
            tok = out.strip().split()
            if tok:
                return tok[0]
    except Exception:
        pass
    return "2"


UA = os.environ.get("RUNCAT_USER_AGENT") or f"claude-cli/{claude_version()} (external, cli)"


# ---------- formatting ----------
def fmt_reset(v):
    """Format a reset time. stdin gives unix epoch (int); the API gives ISO 8601."""
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            dt = datetime.fromtimestamp(int(v))
        else:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt.astimezone().strftime("%-m/%-d %H:%M")
    except Exception:
        return None


def row(title, pct, reset_val):
    if pct is None:
        return None
    label = f"{pct:g}%"
    rl_ = fmt_reset(reset_val)
    if rl_:
        label += f"  ⟳{rl_}"
    return {"title": title, "formattedValue": label, "normalizedValue": round(pct / 100, 4)}


# ---------- OAuth usage (cached, best-effort) ----------
def read_token():
    try:
        blob = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if blob:
            return (json.loads(blob).get("claudeAiOauth") or {}).get("accessToken")
    except Exception:
        pass
    try:
        data = json.loads((CLAUDE_DIR / ".credentials.json").read_text())
        return (data.get("claudeAiOauth") or {}).get("accessToken")
    except Exception:
        return None


def fetch_usage():
    token = read_token()
    if not token:
        return None
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))


def lane_label(lim):
    scope = lim.get("scope") or {}
    model = (scope.get("model") or {}) if isinstance(scope, dict) else {}
    name = model.get("display_name")
    if name:
        return name  # dynamic: "Fable", "Opus", "Sonnet", …
    kind = lim.get("kind")
    return KIND_LABELS.get(kind, kind or "limit")


def extract(usage):
    """Discover every lane from the usage `limits[]` array, in order."""
    lanes = []
    for lim in (usage.get("limits") or []):
        pct = lim.get("percent")
        if pct is None:
            continue
        lanes.append({
            "title": lane_label(lim),
            "pct": pct,
            "reset": lim.get("resets_at"),
            "kind": lim.get("kind"),
        })
    return lanes


cache = {}
try:
    cache = json.loads(CACHE.read_text())
except Exception:
    cache = {}

if not FABLE_OFF and (time.time() - cache.get("fetched_at", 0)) > REFRESH:
    fd = None
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_RDWR, 0o600)
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            got = True
        except Exception:
            got = False
        if got:
            new = dict(cache)
            new["fetched_at"] = time.time()  # bump regardless of outcome → <=1 call / REFRESH
            try:
                usage = fetch_usage()
                if usage:
                    if DEBUG_ON:
                        try:
                            DEBUG.write_text(json.dumps(usage, ensure_ascii=False, indent=2))
                        except Exception:
                            pass
                    got_lanes = extract(usage)
                    if got_lanes:
                        new["lanes"] = got_lanes
                    new["ok"] = True
                    new.pop("err", None)
            except Exception as e:
                new["ok"] = False
                new["err"] = f"{type(e).__name__}: {str(e)[:120]}"
            cache = new
            try:
                tmp = CACHE.with_suffix(".tmp")
                tmp.write_text(json.dumps(cache, ensure_ascii=False))
                os.replace(tmp, CACHE)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass

cached_lanes = cache.get("lanes")
if not isinstance(cached_lanes, list):
    cached_lanes = []


# ---------- build RunCat card ----------
def stdin_overlay(kind):
    """Fresher stdin value for the lanes Claude Code reports directly."""
    if kind == "session" and sin_five_pct is not None:
        return sin_five_pct, sin_five_reset
    if kind == "weekly_all" and sin_seven_pct is not None:
        return sin_seven_pct, sin_seven_reset
    return None


metrics = []
avail = []

if cached_lanes:
    for lane in cached_lanes:
        ov = stdin_overlay(lane.get("kind"))
        pct, reset = ov if ov else (lane.get("pct"), lane.get("reset"))
        m = row(lane.get("title") or lane.get("kind") or "limit", pct, reset)
        if m:
            metrics.append(m)
            avail.append(pct)
else:
    # No API data yet (first run / offline) — show whatever stdin provides.
    for title, pct, reset in (("5h", sin_five_pct, sin_five_reset), ("7d", sin_seven_pct, sin_seven_reset)):
        m = row(title, pct, reset)
        if m:
            metrics.append(m)
            avail.append(pct)

snapshot = {
    "title": "Claude Code",
    "symbol": "staroflife",
    "metrics": metrics,
    "lastUpdatedDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
if avail:
    snapshot["metricsBarValue"] = f"{max(avail):g}%"  # most-constraining lane

OUT.parent.mkdir(parents=True, exist_ok=True)
fd2, tmp2 = tempfile.mkstemp(prefix=".runcat-", dir=str(OUT.parent))
with os.fdopen(fd2, "w", encoding="utf-8") as f:
    json.dump(snapshot, f, ensure_ascii=False)
os.replace(tmp2, OUT)


# ---------- terminal status line ----------
print("  ".join(f"{m['title']} {m['formattedValue'].split(' ')[0]}" for m in metrics) or "Claude Code")
