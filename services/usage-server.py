"""
PyPortal usage proxy server — serves utilization data to the PyPortal display.

Handles both Claude (Anthropic OAuth) and Codex (ChatGPT OAuth) usage data.
Reads tokens from the macOS Keychain (Claude) and ~/.codex/auth.json (Codex),
caches responses for 15 minutes, and serves unified or dedicated endpoints.

GET /health       → health & cache status
GET /claude-usage → Claude usage metrics (alias: /usage)
GET /codex-usage  → Codex usage metrics
GET /all-usage    → Combined Claude & Codex usage metrics
Usage:
    python services/usage-server.py
    PORT=9292 python services/usage-server.py
"""

import hashlib
import json
import os
import getpass
import shutil
import subprocess
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

PORT = int(os.environ.get("PORT", 9292))
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")

# ── Claude configuration ──────────────────────────────────────────────────────
_CLAUDE_KEYCHAIN_SERVICE = os.environ.get("CLAUDE_KEYCHAIN_SERVICE", "Claude Code-credentials")
_CLAUDE_KEYCHAIN_ACCOUNT = os.environ.get("CLAUDE_KEYCHAIN_ACCOUNT", getpass.getuser())
_CLAUDE_KEYCHAIN_PATH = os.environ.get("CLAUDE_KEYCHAIN_PATH", "")
_CLAUDE_AUTH_PATH = Path(
    os.environ.get(
        "CLAUDE_AUTH_PATH",
        Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / ".credentials.json",
    )
)
_CLAUDE_USAGE_URL        = "https://api.anthropic.com/api/oauth/usage"

_CLAUDE_TOKEN_TTL = 300   # re-read Keychain every 5 minutes
_CLAUDE_USAGE_TTL = 900   # cache Anthropic response for 15 minutes

_claude_token_cache = {"token": "", "at": 0.0}
_claude_usage_cache = {"body": b"", "dict": None, "at": 0.0}


# ── Codex configuration ───────────────────────────────────────────────────────
_CODEX_AUTH_PATH = Path(
    os.environ.get(
        "CODEX_AUTH_PATH",
        Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json",
    )
)
_CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

_CODEX_TOKEN_TTL = 300   # check auth.json every 5 minutes (or on mtime change)
_CODEX_USAGE_TTL = 900   # cache OpenAI response for 15 minutes
_STATE_HOME = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
_CODEX_HISTORY_PATH = Path(
    os.environ.get(
        "USAGE_DASHBOARD_STATE_PATH",
        _STATE_HOME / "pyportal-ai-usage" / "codex-daily.json",
    )
)

_codex_token_cache = {"access_token": "", "account_id": "", "at": 0.0, "mtime": 0.0}
_codex_usage_cache = {"body": b"", "dict": None, "at": 0.0}


# ── OpenCode Go configuration ─────────────────────────────────────────────────
_OPENCODE_GO_KEY_ENV = "OPENCODE_GO_API_KEY"
_OPENCODE_GO_KEYCHAIN_SERVICE = os.environ.get("OPENCODE_GO_KEYCHAIN_SERVICE", "PyPortal-opencode-go")
_OPENCODE_GO_KEYCHAIN_ACCOUNT = os.environ.get("OPENCODE_GO_KEYCHAIN_ACCOUNT", getpass.getuser())
_OPENCODE_GO_KEYCHAIN_PATH = os.environ.get("OPENCODE_GO_KEYCHAIN_PATH", "")
_OPENCODE_GO_AUTH_PATH = Path(
    os.environ.get("OPENCODE_AUTH_PATH", str(Path.home() / ".local" / "share" / "opencode" / "auth.json"))
)
_OPENCODE_GO_USAGE_URL = "https://opencode.ai/inference/go/v1/usage"

_OPENCODE_GO_TOKEN_TTL = 300   # check auth.json every 5 minutes (or on mtime change)
_OPENCODE_GO_USAGE_TTL = 900   # cache OpenCode Go response for 15 minutes
_OPENCODE_GO_TIMEOUT   = 10    # keep single-threaded /all-usage responsive
_OPENCODE_GO_AUTH_COOLDOWN = 300

_opencode_go_token_cache = {"api_key": "", "at": 0.0, "mtime": 0.0}
_opencode_go_usage_cache = {"body": b"", "dict": None, "at": 0.0, "cooldown_until": 0.0, "last_error_status": 0}


# ── Claude helpers ────────────────────────────────────────────────────────────

def _get_claude_token(force=False):
    now = time.monotonic()
    if not force and _claude_token_cache["token"] and now - _claude_token_cache["at"] < _CLAUDE_TOKEN_TTL:
        return _claude_token_cache["token"]
    token = ""
    if shutil.which("security"):
        try:
            command = [
                "security", "find-generic-password",
                "-a", _CLAUDE_KEYCHAIN_ACCOUNT,
                "-s", _CLAUDE_KEYCHAIN_SERVICE,
                "-w",
            ]
            if _CLAUDE_KEYCHAIN_PATH:
                command.append(_CLAUDE_KEYCHAIN_PATH)
            raw = subprocess.check_output(
                command,
                text=True,
                stderr=subprocess.DEVNULL,
            )
            data = json.loads(raw.strip())
            token = (data.get("claudeAiOauth") or {}).get("accessToken", "")
        except Exception as e:
            print(f"Claude Keychain read error: {e}", flush=True)
    # AIDEV-NOTE: Claude Code can retain an empty Keychain entry while using file credentials.
    if not token:
        try:
            data = json.loads(_CLAUDE_AUTH_PATH.read_text())
            token = (data.get("claudeAiOauth") or {}).get("accessToken", "")
        except (OSError, ValueError, TypeError, AttributeError):
            token = ""
    _claude_token_cache["token"] = token
    _claude_token_cache["at"] = now
    return token


def _fetch_claude(token):
    r = requests.get(
        _CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "claude-code/2.1.132",
        },
        timeout=15,
    )
    return r.status_code, r.headers, r.content


def _resets_in(resets_at_str):
    if not resets_at_str:
        return 0
    try:
        dt = datetime.fromisoformat(resets_at_str)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0, int(delta))
    except Exception:
        return 0


def _limit_meta(data, kind, display_name=None):
    # AIDEV-NOTE: Anthropic's payload currently exposes Fable in limits[] as a
    # weekly_scoped entry rather than as a dedicated top-level seven_day_* key.
    for limit in data.get("limits") or []:
        if limit.get("kind") != kind:
            continue
        if display_name:
            scope = limit.get("scope") or {}
            model = (scope.get("model") or {}).get("display_name", "")
            if model.lower() != display_name.lower():
                continue
        percent = limit.get("percent")
        return {
            "percent": None if percent is None else round(percent),
            "resets_in": _resets_in(limit.get("resets_at")),
            "active": bool(limit.get("is_active", False)),
            "severity": limit.get("severity") or "",
        }
    return {}


def _build_claude_dict(raw):
    data = json.loads(raw)

    def _pct(key):
        section = data.get(key) or {}
        value = section.get("utilization")
        return None if value is None else round(value)

    def _reset(key):
        return _resets_in((data.get(key) or {}).get("resets_at"))

    ex = data.get("extra_usage") or {}
    session = _limit_meta(data, "session")
    weekly = _limit_meta(data, "weekly_all")
    fable = _limit_meta(data, "weekly_scoped", "Fable")
    return {
        "five_h":           _pct("five_hour"),
        "five_h_resets_in": _reset("five_hour"),
        "five_h_active":    session.get("active", False),
        "five_h_severity":  session.get("severity", ""),
        "seven_d":          _pct("seven_day"),
        "seven_d_resets_in": _reset("seven_day"),
        "seven_d_active":   weekly.get("active", False),
        "seven_d_severity": weekly.get("severity", ""),
        "fable":            fable.get("percent"),
        "fable_resets_in":  fable.get("resets_in", 0),
        "fable_active":     fable.get("active", False),
        "fable_severity":   fable.get("severity", ""),
        "sonnet":           _pct("seven_day_sonnet"),
        "extra_used":       ex.get("used_credits", 0) or 0,
        "extra_lim":        ex.get("monthly_limit", 0) or 0,
        "extra_pct":        round(ex.get("utilization", 0) or 0),
    }


def _fetch_claude_usage():
    """Fetch Claude usage, update cache, and return (status, headers, body_bytes, parsed_dict)."""
    now = time.monotonic()
    if _claude_usage_cache["body"] and now - _claude_usage_cache["at"] < _CLAUDE_USAGE_TTL:
        return 200, {}, _claude_usage_cache["body"], _claude_usage_cache["dict"]

    token = _get_claude_token()
    if not token:
        if _claude_usage_cache["body"]:
            return 200, {}, _claude_usage_cache["body"], _claude_usage_cache["dict"]
        return 503, {}, json.dumps({"error": "Could not read Claude token from Keychain"}).encode(), None

    try:
        status, headers, raw = _fetch_claude(token)
    except Exception as e:
        print(f"Claude fetch error: {e}", flush=True)
        if _claude_usage_cache["body"]:
            return 200, {}, _claude_usage_cache["body"], _claude_usage_cache["dict"]
        return 502, {}, json.dumps({"error": "Claude network error"}).encode(), None

    if status == 401:
        token = _get_claude_token(force=True)
        try:
            status, headers, raw = _fetch_claude(token)
        except Exception as e:
            print(f"Claude retry fetch error: {e}", flush=True)
            if _claude_usage_cache["body"]:
                return 200, {}, _claude_usage_cache["body"], _claude_usage_cache["dict"]
            return 502, {}, json.dumps({"error": "Claude network error after token refresh"}).encode(), None

    if status == 429:
        retry_after = headers.get("Retry-After", "")
        body = raw or json.dumps({"error": "rate_limited"}).encode()
        return 429, ({"Retry-After": retry_after} if retry_after else {}), body, None

    if status != 200 or not raw:
        if _claude_usage_cache["body"]:
            return 200, {}, _claude_usage_cache["body"], _claude_usage_cache["dict"]
        return 502, {}, json.dumps({"error": f"Claude upstream {status}"}).encode(), None

    try:
        parsed = _build_claude_dict(raw)
        body = json.dumps(parsed).encode()
    except Exception as e:
        print(f"Claude parse error: {e}", flush=True)
        if _claude_usage_cache["body"]:
            return 200, {}, _claude_usage_cache["body"], _claude_usage_cache["dict"]
        return 502, {}, json.dumps({"error": "Bad upstream response from Claude"}).encode(), None

    _claude_usage_cache["body"] = body
    _claude_usage_cache["dict"] = parsed
    _claude_usage_cache["at"] = now
    return 200, {}, body, parsed


# ── Codex helpers ─────────────────────────────────────────────────────────────

def _get_codex_token(force=False):
    now = time.monotonic()
    mtime = 0.0
    if _CODEX_AUTH_PATH.exists():
        try:
            mtime = _CODEX_AUTH_PATH.stat().st_mtime
        except Exception:
            pass

    if (
        not force
        and _codex_token_cache["access_token"]
        and mtime == _codex_token_cache["mtime"]
        and now - _codex_token_cache["at"] < _CODEX_TOKEN_TTL
    ):
        return _codex_token_cache["access_token"], _codex_token_cache["account_id"]

    access_token = ""
    account_id = ""
    try:
        if _CODEX_AUTH_PATH.exists():
            data = json.loads(_CODEX_AUTH_PATH.read_text())
            tokens = data.get("tokens") or {}
            access_token = tokens.get("access_token", "")
            account_id = tokens.get("account_id", "")
    except Exception as e:
        print(f"Codex auth file read error: {e}", flush=True)

    _codex_token_cache["access_token"] = access_token
    _codex_token_cache["account_id"] = account_id
    _codex_token_cache["mtime"] = mtime
    _codex_token_cache["at"] = now
    return access_token, account_id


def _fetch_codex(token, account_id):
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "codex-cli/0.148.0",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    r = requests.get(_CODEX_USAGE_URL, headers=headers, timeout=15)
    return r.status_code, r.headers, r.content


def _format_codex_plan(plan_type):
    """Normalize verbose Codex plan_type to fit the PyPortal UI (<= 8 chars)."""
    if not plan_type:
        return "team"
    p = str(plan_type).lower().strip()
    if "business" in p:
        return "business"
    if "enterprise" in p:
        return "enterprise"
    if "team" in p:
        return "team"
    if "pro" in p and "prolite" not in p:
        return "pro"
    if "plus" in p:
        return "plus"
    if "free" in p:
        return "free"
    cleaned = p.replace("self_serve_", "").replace("_prolite", "").replace("_", " ")
    return cleaned[:8]


def _observe_codex_day(data, now=None):
    """Track observed weekly-quota increases, never infer unobserved daily usage."""
    now = time.time() if now is None else now
    day = datetime.fromtimestamp(now).astimezone().date().isoformat()
    windows = data.get("rate_limit") or {}
    weekly = next((w for w in (windows.get("primary_window"), windows.get("secondary_window"))
                   if w and w.get("limit_window_seconds") == 604800), None)
    identity = str(data.get("account_id", "")) + ":" + str(data.get("user_id", ""))
    if not weekly or weekly.get("used_percent") is None or identity == ":":
        return None
    pct = float(weekly["used_percent"])
    reset = weekly.get("reset_at")
    if not reset or not 0 <= pct <= 100:
        return None
    account = hashlib.sha256(identity.encode()).hexdigest()
    current = {"day": day, "account": account, "pct": pct, "reset": reset,
               "points": 0, "samples": 1, "at": now}
    try:
        previous = json.loads(_CODEX_HISTORY_PATH.read_text())
        # AIDEV-NOTE: Midnight, quota resets, and account changes establish a new baseline.
        # Missing observations cannot be assigned to a day; show observed usage only.
        if (previous["day"] == day and previous["account"] == account
                and previous["reset"] == reset and previous["at"] <= now
                and pct >= previous["pct"]):
            current["points"] = previous["points"] + pct - previous["pct"]
            current["samples"] = previous["samples"] + 1
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        _CODEX_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = _CODEX_HISTORY_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(current))
        temporary.replace(_CODEX_HISTORY_PATH)
    except OSError:
        print("Codex daily history could not be saved", flush=True)
        return None
    return round(current["points"]) if current["samples"] > 1 else None


def _build_codex_dict(raw):
    data = json.loads(raw)
    rate_limit = data.get("rate_limit") or {}
    primary = rate_limit.get("primary_window") or {}
    secondary = rate_limit.get("secondary_window") or {}

    seven_d = None
    seven_d_reset = 0
    five_h = None
    five_h_reset = 0

    for w in [primary, secondary]:
        if not w:
            continue
        window_sec = w.get("limit_window_seconds", 0)
        pct = w.get("used_percent")
        pct = None if pct is None else round(pct)
        reset_in = w.get("reset_after_seconds", 0)

        if window_sec >= 86400:
            seven_d = pct
            seven_d_reset = reset_in
        elif window_sec > 0:
            five_h = pct
            five_h_reset = reset_in

    credits = data.get("credits") or {}
    reset_credits = (data.get("rate_limit_reset_credits") or {}).get("available_count", 0)

    return {
        "plan":             _format_codex_plan(data.get("plan_type", "team")),
        "allowed":          rate_limit.get("allowed", True),
        "limit_reached":    rate_limit.get("limit_reached", False),
        "five_h":           five_h,
        "five_h_resets_in": five_h_reset,
        "seven_d":          seven_d,
        "seven_d_resets_in": seven_d_reset,
        "reset_credits":    reset_credits,
        "applicable_reset_credits": (data.get("rate_limit_reset_credits") or {}).get("applicable_available_count"),
        "has_credits":      credits.get("has_credits", False),
        "approx_local_msg": credits.get("approx_local_messages"),
        "approx_cloud_msg": credits.get("approx_cloud_messages"),
    }


def _fetch_codex_usage():
    """Fetch Codex usage, update cache, and return (status, headers, body_bytes, parsed_dict)."""
    now = time.monotonic()
    if _codex_usage_cache["body"] and now - _codex_usage_cache["at"] < _CODEX_USAGE_TTL:
        return 200, {}, _codex_usage_cache["body"], _codex_usage_cache["dict"]

    token, account_id = _get_codex_token()
    if not token:
        if _codex_usage_cache["body"]:
            return 200, {}, _codex_usage_cache["body"], _codex_usage_cache["dict"]
        return 503, {}, json.dumps({"error": "Could not read Codex token from auth.json"}).encode(), None

    try:
        status, headers, raw = _fetch_codex(token, account_id)
    except Exception as e:
        print(f"Codex fetch error: {e}", flush=True)
        if _codex_usage_cache["body"]:
            return 200, {}, _codex_usage_cache["body"], _codex_usage_cache["dict"]
        return 502, {}, json.dumps({"error": "Codex network error"}).encode(), None

    if status == 401:
        token, account_id = _get_codex_token(force=True)
        try:
            status, headers, raw = _fetch_codex(token, account_id)
        except Exception as e:
            print(f"Codex retry fetch error: {e}", flush=True)
            if _codex_usage_cache["body"]:
                return 200, {}, _codex_usage_cache["body"], _codex_usage_cache["dict"]
            return 502, {}, json.dumps({"error": "Codex network error after token refresh"}).encode(), None

    if status == 429:
        retry_after = headers.get("Retry-After", "")
        body = raw or json.dumps({"error": "rate_limited"}).encode()
        return 429, ({"Retry-After": retry_after} if retry_after else {}), body, None

    if status != 200 or not raw:
        if _codex_usage_cache["body"]:
            return 200, {}, _codex_usage_cache["body"], _codex_usage_cache["dict"]
        return 502, {}, json.dumps({"error": f"Codex upstream {status}"}).encode(), None

    try:
        parsed = _build_codex_dict(raw)
        parsed["daily_points"] = _observe_codex_day(json.loads(raw))
        body = json.dumps(parsed).encode()
    except Exception as e:
        print(f"Codex parse error: {e}", flush=True)
        if _codex_usage_cache["body"]:
            return 200, {}, _codex_usage_cache["body"], _codex_usage_cache["dict"]
        return 502, {}, json.dumps({"error": "Bad upstream response from Codex"}).encode(), None

    _codex_usage_cache["body"] = body
    _codex_usage_cache["dict"] = parsed
    _codex_usage_cache["at"] = now
    return 200, {}, body, parsed


# ── OpenCode Go helpers ───────────────────────────────────────────────────────

def _read_opencode_go_keychain():
    if not shutil.which("security"):
        return ""
    try:
        command = [
            "security", "find-generic-password",
            "-a", _OPENCODE_GO_KEYCHAIN_ACCOUNT,
            "-s", _OPENCODE_GO_KEYCHAIN_SERVICE,
            "-w",
        ]
        if _OPENCODE_GO_KEYCHAIN_PATH:
            command.append(_OPENCODE_GO_KEYCHAIN_PATH)
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def _get_opencode_go_key(force=False):
    now = time.monotonic()
    env_key = os.environ.get(_OPENCODE_GO_KEY_ENV, "").strip()
    if env_key:
        if env_key != _opencode_go_token_cache["api_key"]:
            _opencode_go_usage_cache["cooldown_until"] = 0.0
        _opencode_go_token_cache["api_key"] = env_key
        _opencode_go_token_cache["at"] = now
        return env_key

    mtime = 0.0
    if _OPENCODE_GO_AUTH_PATH.exists():
        try:
            mtime = _OPENCODE_GO_AUTH_PATH.stat().st_mtime
        except Exception:
            pass

    if (
        not force
        and _opencode_go_token_cache["api_key"]
        and mtime == _opencode_go_token_cache["mtime"]
        and now - _opencode_go_token_cache["at"] < _OPENCODE_GO_TOKEN_TTL
    ):
        return _opencode_go_token_cache["api_key"]

    api_key = _read_opencode_go_keychain()
    if not api_key:
        try:
            if _OPENCODE_GO_AUTH_PATH.exists():
                data = json.loads(_OPENCODE_GO_AUTH_PATH.read_text())
                entry = data.get("opencode-go") if isinstance(data, dict) else None
                if isinstance(entry, dict):
                    api_key = str(entry.get("key") or "").strip()
        except Exception as e:
            print(f"OpenCode Go auth file read error: {e}", flush=True)

    if api_key and api_key != _opencode_go_token_cache["api_key"]:
        _opencode_go_usage_cache["cooldown_until"] = 0.0

    _opencode_go_token_cache["api_key"] = api_key
    _opencode_go_token_cache["mtime"] = mtime
    _opencode_go_token_cache["at"] = now
    return api_key


def _fetch_opencode_go(api_key):
    r = requests.get(
        _OPENCODE_GO_USAGE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "pyportal-usage-proxy/1.0",
        },
        timeout=_OPENCODE_GO_TIMEOUT,
    )
    return r.status_code, r.headers, r.content


def _build_opencode_go_dict(raw):
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else data
    if not any(isinstance(usage.get(k), dict) for k in ("rolling", "weekly", "monthly")):
        raise ValueError("Missing OpenCode Go usage windows")

    def _window(name):
        w = usage.get(name)
        if not isinstance(w, dict):
            return None, 0, ""
        pct = w.get("percent")
        pct = None if pct is None else max(0, min(100, round(pct)))
        resets_in = _resets_in(w.get("resetsAt") or w.get("resets_at"))
        status = str(w.get("status") or "").lower()
        return pct, resets_in, status

    five_h, five_h_reset, five_h_status = _window("rolling")
    seven_d, seven_d_reset, seven_d_status = _window("weekly")
    monthly, monthly_reset, monthly_status = _window("monthly")
    limit_reached = any(s == "rate-limited" for s in (five_h_status, seven_d_status, monthly_status))

    return {
        "five_h": five_h,
        "five_h_resets_in": five_h_reset,
        "five_h_status": five_h_status,
        "seven_d": seven_d,
        "seven_d_resets_in": seven_d_reset,
        "seven_d_status": seven_d_status,
        "monthly": monthly,
        "monthly_resets_in": monthly_reset,
        "monthly_status": monthly_status,
        "limit_reached": limit_reached,
        "observed_at": int(time.time()),
    }


def _retry_after_seconds(headers, default=_OPENCODE_GO_USAGE_TTL):
    raw = (headers or {}).get("Retry-After", "")
    try:
        secs = int(str(raw).strip())
        if secs > 0:
            return secs
    except (ValueError, TypeError):
        pass
    return default


def _fetch_opencode_go_usage():
    """Fetch OpenCode Go usage, update cache, and return (status, headers, body_bytes, parsed_dict)."""
    now = time.monotonic()
    if _opencode_go_usage_cache["body"] and now - _opencode_go_usage_cache["at"] < _OPENCODE_GO_USAGE_TTL:
        return 200, {}, _opencode_go_usage_cache["body"], _opencode_go_usage_cache["dict"]

    api_key = _get_opencode_go_key()
    if not api_key:
        if _opencode_go_usage_cache["body"]:
            return 200, {}, _opencode_go_usage_cache["body"], _opencode_go_usage_cache["dict"]
        return 503, {}, json.dumps({"error": "Could not read OpenCode Go API key"}).encode(), None

    if now < _opencode_go_usage_cache["cooldown_until"]:
        if _opencode_go_usage_cache["body"]:
            return 200, {}, _opencode_go_usage_cache["body"], _opencode_go_usage_cache["dict"]
        err_status = _opencode_go_usage_cache["last_error_status"] or 429
        return err_status, {}, json.dumps({"error": f"OpenCode Go cooldown ({err_status})"}).encode(), None

    try:
        status, headers, raw = _fetch_opencode_go(api_key)
    except Exception as e:
        print(f"OpenCode Go fetch error: {e}", flush=True)
        if _opencode_go_usage_cache["body"]:
            return 200, {}, _opencode_go_usage_cache["body"], _opencode_go_usage_cache["dict"]
        return 502, {}, json.dumps({"error": "OpenCode Go network error"}).encode(), None

    if status == 401:
        refreshed_key = _get_opencode_go_key(force=True)
        if refreshed_key and refreshed_key != api_key:
            try:
                status, headers, raw = _fetch_opencode_go(refreshed_key)
            except Exception as e:
                print(f"OpenCode Go retry fetch error: {e}", flush=True)
                if _opencode_go_usage_cache["body"]:
                    return 200, {}, _opencode_go_usage_cache["body"], _opencode_go_usage_cache["dict"]
                return 502, {}, json.dumps({"error": "OpenCode Go network error after key refresh"}).encode(), None

    if status in (401, 403):
        _opencode_go_usage_cache["cooldown_until"] = now + _OPENCODE_GO_AUTH_COOLDOWN
        _opencode_go_usage_cache["last_error_status"] = status
        return status, {}, json.dumps({"error": f"OpenCode Go auth error ({status})"}).encode(), None

    if status == 429:
        retry_after = headers.get("Retry-After", "")
        _opencode_go_usage_cache["cooldown_until"] = now + _retry_after_seconds(headers)
        _opencode_go_usage_cache["last_error_status"] = 429
        body = raw or json.dumps({"error": "rate_limited"}).encode()
        return 429, ({"Retry-After": retry_after} if retry_after else {}), body, None

    if status != 200 or not raw:
        if _opencode_go_usage_cache["body"]:
            return 200, {}, _opencode_go_usage_cache["body"], _opencode_go_usage_cache["dict"]
        return 502, {}, json.dumps({"error": f"OpenCode Go upstream {status}"}).encode(), None

    try:
        parsed = _build_opencode_go_dict(raw)
        body = json.dumps(parsed).encode()
    except Exception as e:
        print(f"OpenCode Go parse error: {e}", flush=True)
        if _opencode_go_usage_cache["body"]:
            return 200, {}, _opencode_go_usage_cache["body"], _opencode_go_usage_cache["dict"]
        return 502, {}, json.dumps({"error": "Bad upstream response from OpenCode Go"}).encode(), None

    _opencode_go_usage_cache["body"] = body
    _opencode_go_usage_cache["dict"] = parsed
    _opencode_go_usage_cache["at"] = now
    _opencode_go_usage_cache["cooldown_until"] = 0.0
    _opencode_go_usage_cache["last_error_status"] = 0
    return 200, {}, body, parsed


# ── HTTP Request Handler ──────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._health()
        elif self.path in ("/usage", "/claude-usage"):
            self._claude_usage()
        elif self.path == "/codex-usage":
            self._codex_usage()
        elif self.path == "/opencode-go-usage":
            self._opencode_go_usage()
        elif self.path == "/all-usage":
            self._all_usage()
        else:
            self.send_error(404)

    def _health(self):
        now = time.monotonic()
        claude_age = int(now - _claude_usage_cache["at"]) if _claude_usage_cache["body"] else -1
        codex_age = int(now - _codex_usage_cache["at"]) if _codex_usage_cache["body"] else -1
        opencode_go_age = int(now - _opencode_go_usage_cache["at"]) if _opencode_go_usage_cache["body"] else -1
        self._json(200, {
            "ok": True,
            "has_token": bool(
                _claude_token_cache["token"]
                or _codex_token_cache["access_token"]
                or _opencode_go_token_cache["api_key"]
            ),
            "cache_age_s": claude_age,
            "claude": {
                "has_token": bool(_claude_token_cache["token"]),
                "cache_age_s": claude_age,
            },
            "codex": {
                "has_token": bool(_codex_token_cache["access_token"]),
                "cache_age_s": codex_age,
            },
            "opencode_go": {
                "has_token": bool(_opencode_go_token_cache["api_key"]),
                "cache_age_s": opencode_go_age,
            },
        })

    def _claude_usage(self):
        status, headers, body, _ = _fetch_claude_usage()
        self._respond(status, body, headers=headers)

    def _codex_usage(self):
        status, headers, body, _ = _fetch_codex_usage()
        self._respond(status, body, headers=headers)

    def _opencode_go_usage(self):
        status, headers, body, _ = _fetch_opencode_go_usage()
        self._respond(status, body, headers=headers)

    def _all_usage(self):
        # Fetch all configured providers (respecting their respective caches)
        c_status, _, c_body, c_dict = _fetch_claude_usage()
        x_status, _, x_body, x_dict = _fetch_codex_usage()
        g_status, _, g_body, g_dict = _fetch_opencode_go_usage()

        combined = {
            "claude": c_dict if c_status == 200 else {"error": c_status},
            "codex":  x_dict if x_status == 200 else {"error": x_status},
            "opencode_go": g_dict if g_status == 200 else {"error": g_status},
            "updated_at": int(time.time()),
        }
        self._json(200, combined)

    def _json(self, code, data):
        self._respond(code, json.dumps(data).encode())

    def _respond(self, code, body, content_type="application/json", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    _get_claude_token()     # warm Claude token cache
    _get_codex_token()      # warm Codex token cache
    _get_opencode_go_key()  # warm OpenCode Go key cache
    print(f"PyPortal usage proxy listening on {BIND_HOST}:{PORT}", flush=True)
    HTTPServer((BIND_HOST, PORT), Handler).serve_forever()
