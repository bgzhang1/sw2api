#!/usr/bin/env python3
"""
stagewise Reverse Proxy (Python)

Full lifecycle:
  1. Login (email OTP) -> get session token
  2. Save token locally (config.json)
  3. Auto-refresh session every 5 minutes
  4. Reverse-proxy LLM requests -> https://api.stagewise.io
  5. OpenAI-compatible API on localhost
  6. Multi-account support with 3 strategies:
     - specific:  use activeAccount only (default)
     - fill_first: burn through one account at a time
     - round_robin: distribute across all available accounts

Usage:
  python proxy.py --login          Interactive email OTP login
  python proxy.py --status         Check session status
  python proxy.py --logout         Sign out and clear token
  python proxy.py [--port 11434]   Start reverse proxy (default)
  python proxy.py --strategy fill_first  Use fill-first strategy

Compatible with OpenAI client libraries:
  openai.api_base = "http://localhost:11434/v1"
"""

import argparse
import json
import os
import sys
import ssl
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, urlencode, parse_qs
from http.client import HTTPSConnection
from pathlib import Path
import call_log
import health_tracker
import key_manager
import usage_store

API_ORIGIN = "https://api.stagewise.io"
SYSTEM_PROMPT = "The following sections define your identity and operating environment:- `<soul>` — Identity, behavior rules, and values- `<environment>` — Tools, interfaces, file system, and skill system- `<output-style>` — Response formatting and special protocols- `<authorities>` — Trust hierarchy and security model### Priority Hierarchy1. **`plugins/{id}/SKILL.md`** — Core intrinsic knowledge. Always prefer.2. **`globalskills-sw/*`** — User-level skills from `~/.stagewise/skills/`. Personal defaults across all workspaces.3. **`{WORKSPACE}/.stagewise/skills/*`** — Workspace-specific, created for you. Overrides general skills.4. **`globalskills-agents/*`** — Cross-agent user-level skills from `~/.agents/skills/`.5. **`{WORKSPACE}/.agents/skills/*`** — General skills shared with other agents.## AGENTS.md (Legacy)Inside a workspace, an `AGENTS.md` file at the workspace root may carry legacy project documentation written for previous coding agents. **Ignore this file unless you already have it loaded in your context** — the canonical project memo lives at `.stagewise/WORKSPACE.md` (see the WORKSPACE.md section below). Never read `AGENTS.md` proactively to warm up on a project; rely on `<agents-md>` entries that already surface it."
AUTH_BASE = "/v1/auth"
SESSION_REFRESH_S = 300
DEFAULT_PORT = 11434
BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "data"
CONFIG_PATH = CONFIG_DIR / "config.json"

# ─── Strategy constants ─────────────────────────────────────────
STRATEGY_SPECIFIC = "specific"
STRATEGY_FILL_FIRST = "fill_first"
STRATEGY_ROUND_ROBIN = "round_robin"

# Cooldown durations (seconds)
COOLDOWN_429_BASE = 15
COOLDOWN_429_MAX = 1800
COOLDOWN_401 = 1800
COOLDOWN_5XX = 30


class AccountState:
    """Per-account cooldown / banned state.

    Cooldown (temporary, auto-recovers):  429 / 401 / 5xx
    Banned (permanent, manual clear only): 403
    """

    def __init__(self, email):
        self.email = email
        self.unavailable = False
        self.next_retry_at = 0.0
        self.backoff_level = 0
        self.last_status = 0
        self.banned = False
        self.banned_reason = ""

    def apply_cooldown(self, status):
        now = time.time()
        self.last_status = status
        if status == 403:
            self.backoff_level = 0
            self.next_retry_at = now + COOLDOWN_401
            self.unavailable = True
        elif status == 429:
            self.backoff_level += 1
            delay = min(COOLDOWN_429_BASE * (2 ** self.backoff_level), COOLDOWN_429_MAX)
            self.next_retry_at = now + delay
            self.unavailable = True
        elif status == 401:
            self.backoff_level = 0
            self.next_retry_at = now + COOLDOWN_401
            self.unavailable = True
        elif 500 <= status < 600:
            self.backoff_level = 0
            self.next_retry_at = now + COOLDOWN_5XX
            self.unavailable = True
        else:
            self.unavailable = False
            self.next_retry_at = 0.0
            self.backoff_level = 0

    def unban(self):
        self.banned = False
        self.banned_reason = ""
        self.unavailable = False
        self.next_retry_at = 0.0

    def is_available(self, now=None):
        if self.banned:
            return False
        if now is None:
            now = time.time()
        if not self.unavailable:
            return True
        if now >= self.next_retry_at:
            self.unavailable = False
            self.backoff_level = 0
            return True
        return False


# Module-level account state (thread-safe)
_account_states_lock = threading.Lock()
_account_states = {}
_rr_cursors = {}
_rr_lock = threading.Lock()
_config_lock = threading.Lock()


def get_account_state(email):
    with _account_states_lock:
        if email not in _account_states:
            _account_states[email] = AccountState(email)
        return _account_states[email]


def get_available_accounts(cfg, now=None):
    """Return list of (email, account_info) for accounts not in cooldown, sorted."""
    if now is None:
        now = time.time()
    accounts = cfg.get("accounts", {})
    available = []
    for email in accounts:
        acct = accounts[email]
        if acct.get("disabled"):
            continue
        state = get_account_state(email)
        if state.is_available(now):
            available.append((email, acct))
    available.sort(key=lambda x: x[0])
    return available


def select_account(cfg, strategy, specific_email=None):
    """Select an account based on the strategy.
    Returns (email, account_info) or (None, None) if none available.
    """
    accounts = cfg.get("accounts", {})
    if not accounts:
        return None, None

    if strategy == STRATEGY_SPECIFIC:
        email = specific_email or cfg.get("activeAccount")
        if email and email in accounts:
            acct = accounts[email]
            if not acct.get("disabled"):
                state = get_account_state(email)
                if state.is_available():
                    return email, acct
        return None, None

    available = get_available_accounts(cfg)
    if not available:
        return None, None

    if strategy == STRATEGY_FILL_FIRST:
        return available[0]

    if strategy == STRATEGY_ROUND_ROBIN:
        with _rr_lock:
            idx = _rr_cursors.get("__global__", 0)
            email, acct = available[idx % len(available)]
            _rr_cursors["__global__"] = idx + 1
        return email, acct

    email = cfg.get("activeAccount")
    if email and email in accounts:
        acct = accounts[email]
        if not acct.get("disabled"):
            return email, acct
    return None, None


# ─── Config ─────────────────────────────────────────────────────

MODELS = [
    {"id": "claude-fable-5", "object": "model", "owned_by": "anthropic"},
    {"id": "claude-opus-4.8", "object": "model", "owned_by": "anthropic"},
    {"id": "claude-opus-4.7", "object": "model", "owned_by": "anthropic"},
    {"id": "claude-opus-4.6", "object": "model", "owned_by": "anthropic"},
    {"id": "claude-sonnet-4.6", "object": "model", "owned_by": "anthropic"},
    {"id": "claude-haiku-4.5", "object": "model", "owned_by": "anthropic"},
    {"id": "gpt-5.5", "object": "model", "owned_by": "openai"},
    {"id": "gpt-5.4", "object": "model", "owned_by": "openai"},
    {"id": "gpt-5.3-codex", "object": "model", "owned_by": "openai"},
    {"id": "gpt-5.3-chat", "object": "model", "owned_by": "openai"},
    {"id": "gpt-5.4-mini", "object": "model", "owned_by": "openai"},
    {"id": "gpt-5.4-nano", "object": "model", "owned_by": "openai"},
    {"id": "gemini-3.1-pro-preview", "object": "model", "owned_by": "google"},
    {"id": "gemini-3.5-flash", "object": "model", "owned_by": "google"},
    {"id": "gemini-3-flash-preview", "object": "model", "owned_by": "google"},
    {"id": "gemini-3.1-flash-lite", "object": "model", "owned_by": "google"},
    {"id": "kimi-k2.7-code", "object": "model", "owned_by": "moonshotai"},
    {"id": "kimi-k2.6", "object": "model", "owned_by": "moonshotai"},
    {"id": "kimi-k2.5", "object": "model", "owned_by": "moonshotai"},
    {"id": "qwen3-32b", "object": "model", "owned_by": "alibaba"},
    {"id": "qwen3-coder-30b-a3b-instruct", "object": "model", "owned_by": "alibaba"},
    {"id": "deepseek-v4-pro", "object": "model", "owned_by": "deepseek"},
    {"id": "deepseek-v4-flash", "object": "model", "owned_by": "deepseek"},
    {"id": "glm-5.2", "object": "model", "owned_by": "z-ai"},
    {"id": "glm-5.1", "object": "model", "owned_by": "z-ai"},
    {"id": "glm-5v-turbo", "object": "model", "owned_by": "z-ai"},
    {"id": "minimax-m3", "object": "model", "owned_by": "minimax"},
    {"id": "minimax-m2.7", "object": "model", "owned_by": "minimax"},
    {"id": "MiniMax-M2", "object": "model", "owned_by": "minimax"},
    {"id": "mimo-v2.5-pro", "object": "model", "owned_by": "xiaomi-mimo"},
    {"id": "mimo-v2.5", "object": "model", "owned_by": "xiaomi-mimo"},
]


def ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
        if "token" in cfg and "accounts" not in cfg:
            email = (cfg.get("user") or {}).get("email", "default")
            cfg["accounts"] = {email: {"token": cfg["token"], "user": cfg.get("user")}}
            cfg["activeAccount"] = email
            save_config(cfg)
        cfg.setdefault("strategy", STRATEGY_SPECIFIC)
        return cfg
    except Exception:
        return {"token": None, "user": None, "port": DEFAULT_PORT, "strategy": STRATEGY_SPECIFIC}


def save_config(cfg):
    with _config_lock:
        ensure_config_dir()
        clean = {k: v for k, v in cfg.items() if k not in ("token", "user")}
        CONFIG_PATH.write_text(json.dumps(clean, indent=2, ensure_ascii=False), "utf-8")


def get_active_token(cfg=None):
    if cfg is None:
        cfg = load_config()
    active = cfg.get("activeAccount")
    if active and active in cfg.get("accounts", {}):
        return cfg["accounts"][active].get("token")
    return cfg.get("token")


# ─── Auth helpers ───────────────────────────────────────────────

def https_request(method, path, headers=None, body=None, timeout=30):
    u = urlparse(API_ORIGIN)
    conn = HTTPSConnection(u.hostname, u.port or 443, timeout=timeout)

    req_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "stagewise-proxy/1.0 (Python)",
    }
    if headers:
        req_headers.update(headers)

    req_body = None
    if body is not None:
        if isinstance(body, (dict, list)):
            req_body = json.dumps(body, ensure_ascii=False)
        else:
            req_body = str(body)
        req_headers["Content-Length"] = str(len(req_body.encode("utf-8")))

    conn.request(method, path, body=req_body, headers=req_headers)
    resp = conn.getresponse()

    resp_headers = {k.lower(): v for k, v in resp.getheaders()}
    resp_body = resp.read().decode("utf-8", errors="replace")

    result = {
        "status": resp.status,
        "headers": resp_headers,
        "body": resp_body,
    }
    try:
        result["json"] = json.loads(resp_body)
    except Exception:
        result["json"] = None

    conn.close()
    return result


def send_otp(email):
    return https_request(
        "POST",
        f"{AUTH_BASE}/email-otp/send-verification-otp",
        body={"email": email.strip(), "type": "sign-in"},
    )


def verify_otp(email, otp):
    return https_request(
        "POST",
        f"{AUTH_BASE}/sign-in/email-otp",
        body={"email": email.strip(), "otp": otp.strip()},
    )


def refresh_session(token):
    return https_request(
        "GET",
        f"{AUTH_BASE}/get-session",
        headers={"Authorization": f"Bearer {token}"},
    )


def sign_out(token):
    return https_request(
        "POST",
        f"{AUTH_BASE}/sign-out",
        headers={"Authorization": f"Bearer {token}"},
    )


def do_login():
    print()
    print("=" * 42)
    print("   stagewise Proxy - Email OTP Login")
    print("=" * 42)
    print()

    email = input("Email: ").strip()
    if "@" not in email:
        print("Invalid email.")
        sys.exit(1)

    print("\n-> Sending OTP...")
    send_res = send_otp(email)

    err = (send_res.get("json") or {}).get("error")
    if err:
        msg = err.get("message", err) if isinstance(err, dict) else err
        print(f"X Failed to send OTP: {msg}")
        sys.exit(1)
    print("OK OTP sent! Check your inbox.\n")

    otp = input("Enter OTP code: ").strip()

    print("\n-> Verifying OTP...")
    verify_res = verify_otp(email, otp)

    set_auth_header = verify_res["headers"].get("set-auth-token")
    j = verify_res.get("json") or {}
    token = set_auth_header or j.get("token") or (j.get("data") or {}).get("token")
    user = j.get("user") or (j.get("data") or {}).get("user")

    if not token:
        err_msg = (j.get("error") or {}).get("message", j.get("error", "Unknown error"))
        print(f"X Login failed: {err_msg}")
        print(f"  Response: {json.dumps(j, indent=2, ensure_ascii=False)}")
        sys.exit(1)

    cfg = {"token": token, "user": user, "port": DEFAULT_PORT, "strategy": STRATEGY_SPECIFIC}
    save_config(cfg)

    print("OK Login successful!")
    print(f"  Email:  {(user or {}).get('email', email)}")
    print(f"  UserID: {(user or {}).get('id', 'N/A')}")
    print(f"  Token:  {token[:30]}...")
    print(f"  Saved:  {CONFIG_PATH}")
    print("\n-> Ready to start proxy: python proxy.py\n")


# ─── HTTP Handler ───────────────────────────────────────────────

class ProxyHandler(BaseHTTPRequestHandler):
    config = None

    def log_message(self, format, *args):
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {args[0]}")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path == "/v1/models" or self.path == "/models":
            self._handle_models()
            return
        self._proxy_request()

    def do_POST(self):
        self._proxy_request()

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, x-stainless-*, X-Account-Email")

    def _handle_models(self):
        resp = {"object": "list", "data": MODELS}
        body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _select_request_account(self):
        """Select an account for this request based on strategy.
        Returns (email, account_info, token) or (None, None, None).
        """
        cfg = self.__class__.config
        if not cfg:
            return None, None, None

        strategy = cfg.get("strategy", STRATEGY_SPECIFIC)

        specific_email = None
        if strategy == STRATEGY_SPECIFIC:
            specific_email = self.headers.get("X-Account-Email") or cfg.get("activeAccount")

        email, acct = select_account(cfg, strategy, specific_email)
        if email and acct:
            return email, acct, acct.get("token")
        return None, None, None

    def _proxy_request(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        is_chat = "/chat/completions" in self.path
        api_key = key_manager.extract_key_from_header(self.headers.get("Authorization", ""))
        if is_chat or api_key:
            is_local = self.client_address[0] in ("127.0.0.1", "::1", "localhost")
            if not api_key and not is_local:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self._cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({
                    "error": {"message": "API key required. Create one in the WebUI or use --create-key.", "type": "auth_error"}
                }).encode("utf-8"))
                return
            if api_key:
                k = key_manager.validate_key(api_key)
                if k is None:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self._cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({
                    "error": {"message": "Invalid, disabled, or rate-limited API key", "type": "auth_error"}
                }).encode("utf-8"))
                return

        email, acct, token = self._select_request_account()
        if not token:
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] No available account (strategy={self.__class__.config.get('strategy', '?')})")
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "30")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": {
                    "code": "account_unavailable",
                    "message": "All accounts are cooling down",
                }
            }).encode("utf-8"))
            return

        target = urlparse(API_ORIGIN)
        conn = HTTPSConnection(target.hostname, target.port or 443, timeout=120)

        path = self.path
        req_model = "unknown"

        if is_chat:
            path = path.replace("/v1/chat/completions", "/v1/ai/chat/completions")
            upstream_headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ai-sdk/openai-compatible/2.0.47 ai-sdk/provider-utils/4.0.27 runtime/node.js/24",
                "X-Stagewise-Client": "electron/1.11.0",
                "Host": target.hostname,
            }
            if body:
                req_model = "unknown"
                try:
                    req = json.loads(body.decode("utf-8"))
                    req_model = req.get("model", "unknown")
                    req.setdefault("reasoning", {"enabled": True, "effort": "low"})
                    req.setdefault("provider", {"require_parameters": True})
                    req.setdefault("messages", []).insert(0, {"role": "system", "content": SYSTEM_PROMPT})
                    body = json.dumps(req, ensure_ascii=False).encode("utf-8")
                except Exception:
                    pass
            if body:
                upstream_headers["Content-Length"] = str(len(body))
        else:
            upstream_headers = {}
            for key, val in self.headers.items():
                lk = key.lower()
                if lk in ("host", "connection", "proxy-connection", "keep-alive",
                           "transfer-encoding", "content-length"):
                    continue
                upstream_headers[key] = val
            upstream_headers["Authorization"] = f"Bearer {token}"
            upstream_headers["Host"] = target.hostname
            if body and "Content-Type" not in upstream_headers:
                upstream_headers["Content-Type"] = "application/json"
            if body:
                upstream_headers["Content-Length"] = str(len(body))

        try:
            conn.request(self.command, path, body=body, headers=upstream_headers)
            resp = conn.getresponse()

            health_tracker.record_request(success=200 <= resp.status < 400)

            set_auth = resp.getheader("set-auth-token")
            if set_auth:
                ts = time.strftime("%H:%M:%S")
                print(f"[{ts}] Token rotated for {email}")
                cfg = self.__class__.config
                if email in cfg.get("accounts", {}):
                    cfg["accounts"][email]["token"] = set_auth
                    save_config(cfg)

            status = resp.status
            if status in (401, 402, 403, 429) or (500 <= status < 600):
                state = get_account_state(email)
                state.apply_cooldown(status)

            self.send_response(resp.status)
            skip = {"set-auth-token", "connection", "keep-alive", "transfer-encoding"}
            for key, val in resp.getheaders():
                if key.lower() not in skip:
                    self.send_header(key, val)
            self._cors_headers()
            self.end_headers()

            streaming = False
            ct = resp.getheader("content-type", "")
            if "text/event-stream" in ct:
                streaming = True

            tokens_used = 0
            model = "unknown"
            i_tokens = 0
            o_tokens = 0
            if streaming:
                last_data = None
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        last_data = chunk
                    except BrokenPipeError:
                        break
                if last_data:
                    for line in last_data.decode("utf-8", errors="replace").split("\n"):
                        if line.startswith("data: ") and "[DONE]" not in line:
                            try:
                                d = json.loads(line[6:])
                                model = d.get("model", model)
                                u = d.get("usage", {})
                                i_tokens = u.get("prompt_tokens", 0) or i_tokens
                                o_tokens = u.get("completion_tokens", 0) or o_tokens
                                tokens_used = u.get("total_tokens", 0) or tokens_used
                            except Exception:
                                pass
            else:
                data = resp.read()
                self.wfile.write(data)
                if resp.status == 200:
                    try:
                        d = json.loads(data.decode("utf-8", errors="replace"))
                        if d.get("error"):
                            state = get_account_state(email)
                            state.apply_cooldown(429)
                        model = d.get("model", model)
                        u = d.get("usage", {})
                        i_tokens = u.get("prompt_tokens", 0)
                        o_tokens = u.get("completion_tokens", 0)
                        tokens_used = u.get("total_tokens", 0)
                    except Exception:
                        pass

            conn.close()

            if tokens_used > 0:
                usage_store.record_usage(tokens_used)
            if api_key and tokens_used > 0:
                key_manager.record_usage(api_key, tokens_used)
                call_log.record(model, email, i_tokens, o_tokens)
            elif api_key:
                key_manager.record_usage(api_key, 0, 1)
                call_log.record(req_model if is_chat else "unknown", email, 0, 0, "fail")

        except Exception as e:
            health_tracker.record_request(success=False)
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] Upstream error for {email}: {e}")
            if not self.headers_sent:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self._cors_headers()
                self.end_headers()
            self.wfile.write(json.dumps({
                "error": {"message": f"Upstream error: {e}", "type": "proxy_error"}
            }).encode("utf-8"))
            if api_key:
                call_log.record(req_model if is_chat else "unknown", email, 0, 0, "error")


def start_refresh_timer(config):
    def worker():
        while True:
            time.sleep(SESSION_REFRESH_S)
            accounts = list(config.get("accounts", {}).items())
            if not accounts:
                continue
            for email, acct in accounts:
                token = acct.get("token")
                if not token:
                    continue
                try:
                    ts = time.strftime("%H:%M:%S")
                    print(f"[{ts}] -> Refreshing {email}...")
                    res = refresh_session(token)
                    new_token = res["headers"].get("set-auth-token")
                    if new_token:
                        print(f"[{ts}] OK Token rotated for {email}")
                        config["accounts"][email]["token"] = new_token
                        save_config(config)
                    state = get_account_state(email)
                    if res["status"] in (401, 403):
                        print(f"[{ts}] X Session expired for {email} ({res['status']})")
                        config["accounts"][email]["token"] = None
                        state.apply_cooldown(res["status"])
                        save_config(config)
                    elif res["status"] == 200:
                        state.apply_cooldown(200)
                except Exception as e:
                    print(f"[{ts}] X Refresh error for {email}: {e}")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def main():
    parser = argparse.ArgumentParser(description="stagewise Reverse Proxy")
    parser.add_argument("--login", "-l", action="store_true", help="Interactive email OTP login")
    parser.add_argument("--logout", action="store_true", help="Sign out and clear token")
    parser.add_argument("--status", "-s", action="store_true", help="Check session status")
    parser.add_argument("--port", "-p", type=int, default=DEFAULT_PORT, help=f"Proxy port (default: {DEFAULT_PORT})")
    parser.add_argument("--create-key", type=str, metavar="NAME", help="Create a new API key with given name")
    parser.add_argument("--list-keys", action="store_true", help="List all API keys with usage")
    parser.add_argument("--delete-key", type=str, metavar="KEY", help="Delete an API key")
    parser.add_argument("--toggle-key", type=str, metavar="KEY", help="Toggle enable/disable for an API key")
    parser.add_argument("--strategy", type=str,
                        choices=[STRATEGY_SPECIFIC, STRATEGY_FILL_FIRST, STRATEGY_ROUND_ROBIN],
                        help=f"Account selection strategy (default: {STRATEGY_SPECIFIC})")
    args = parser.parse_args()

    if args.create_key:
        key, key_id = key_manager.create_key(args.create_key)
        print(f"OK Created API key:")
        print(f"    Name: {args.create_key}")
        print(f"    Key:  {key}")
        print(f"    !!! SAVE THIS KEY NOW - it will not be shown again !!!")
        return

    if args.list_keys:
        keys = key_manager.list_keys()
        if not keys:
            print("No API keys found.")
            return
        print(f"{'Key':<40} {'Name':<20} {'Status':<10} {'Month Tokens':<15} {'Month Reqs':<12}")
        print("-" * 100)
        for k in keys:
            status = "enabled" if k["enabled"] else "disabled"
            print(f"{k['keyPreview']:<40} {k['name']:<20} {status:<10} {k['month_tokens']}/{k['monthly_token_limit']:<8} {k['month_requests']}/{k['monthly_request_limit']:<5}")
        return

    if args.delete_key:
        if key_manager.delete_key(args.delete_key):
            print(f"OK Deleted key: {args.delete_key[:30]}...")
        else:
            print(f"X Key not found: {args.delete_key[:30]}...")
        return

    if args.toggle_key:
        if key_manager.toggle_key(args.toggle_key):
            print(f"OK Key toggled: {args.toggle_key[:30]}...")
        else:
            print(f"X Key not found: {args.toggle_key[:30]}...")
        return

    if args.login:
        do_login()
        return

    if args.logout:
        cfg = load_config()
        if cfg.get("token"):
            try:
                sign_out(cfg["token"])
            except Exception:
                pass
            cfg["token"] = None
            cfg["user"] = None
            save_config(cfg)
            print("OK Logged out.")
        else:
            print("No active session.")
        return

    if args.status:
        cfg = load_config()
        token = get_active_token(cfg)
        if not token:
            print("Status: not authenticated")
            return
        print("-> Checking session...")
        res = refresh_session(token)
        if res["status"] == 200 and (res.get("json", {}).get("session") or res.get("json", {}).get("user")):
            u = res["json"].get("user") or {}
            print(f"Status: authenticated OK")
            active = cfg.get("activeAccount", "")
            print(f"  Account: {active}")
            print(f"  Strategy: {cfg.get('strategy', STRATEGY_SPECIFIC)}")
            print(f"  User: {u.get('email', cfg.get('user', {}).get('email', 'N/A'))}")
        elif res["status"] in (401, 403):
            print("Status: session expired X - re-login needed")
        else:
            print(f"Status: unknown (HTTP {res['status']})")
        return

    cfg = load_config()

    if args.strategy:
        cfg["strategy"] = args.strategy
        save_config(cfg)

    token = get_active_token(cfg)
    if not token:
        if cfg.get("accounts"):
            print("X No active account. Use --login or set activeAccount in config.")
        else:
            print("X No token found. Run with --login first.")
        sys.exit(1)

    print("-> Validating session...")
    res = refresh_session(token)
    if res["status"] in (401, 403):
        print("X Session expired. Run with --login first.")
        sys.exit(1)

    active = cfg.get("activeAccount")
    new_token = res["headers"].get("set-auth-token")
    if new_token:
        if active and active in cfg.get("accounts", {}):
            cfg["accounts"][active]["token"] = new_token
        else:
            cfg["token"] = new_token
        save_config(cfg)

    start_refresh_timer(cfg)

    ProxyHandler.config = cfg

    port = args.port or cfg.get("port", DEFAULT_PORT)
    server = HTTPServer(("0.0.0.0", port), ProxyHandler)

    print()
    print("=" * 50)
    print(f"   stagewise Proxy - Listening on :{port}")
    print(f"   Upstream: {API_ORIGIN}")
    print(f"   Strategy: {cfg.get('strategy', STRATEGY_SPECIFIC)}")
    print(f"   Accounts: {len(cfg.get('accounts', {}))}")
    print(f"   OpenAI-compatible: http://localhost:{port}/v1")
    print("=" * 50)
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
