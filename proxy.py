#!/usr/bin/env python3
"""
stagewise Reverse Proxy (Python)

Full lifecycle:
  1. Multi-account management with tokens
  2. Reverse-proxy LLM requests -> https://api.stagewise.io
  3. OpenAI-compatible API on localhost
  4. Multi-account support with 2 strategies:
     - specific:  use activeAccount only (default)
     - fill_first: burn through one account at a time

Usage:
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

API_ORIGIN = "https://api.stagewise.io"
SYSTEM_PROMPT = "<soul><environment><authorities>stagewisestagewisestagewise"
DEFAULT_PORT = 11434
BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "data"
CONFIG_PATH = CONFIG_DIR / "config.json"

# ─── Strategy constants ─────────────────────────────────────────
STRATEGY_SPECIFIC = "specific"
STRATEGY_FILL_FIRST = "fill_first"

# Cooldown durations (seconds)
COOLDOWN_429 = 60
COOLDOWN_5XX = 30
DEFAULT_RETRY_AFTER = 30


class AccountState:
    def __init__(self, email):
        self.email = email
        self.banned = False
        self.banned_reason = ""
        self.cooldown_until = 0.0

    def unban(self):
        self.banned = False
        self.banned_reason = ""

    def set_cooldown(self, seconds, now=None):
        if now is None:
            now = time.time()
        self.cooldown_until = now + seconds

    def is_available(self, now=None):
        if self.banned:
            return False
        if now is None:
            now = time.time()
        return now >= self.cooldown_until

    def cooldown_remaining(self, now=None):
        if now is None:
            now = time.time()
        return max(0.0, self.cooldown_until - now)


# Module-level account state (thread-safe)
_account_states_lock = threading.Lock()
_account_states = {}
_config_lock = threading.Lock()


def get_account_state(email):
    with _account_states_lock:
        if email not in _account_states:
            _account_states[email] = AccountState(email)
        return _account_states[email]


def handle_upstream_status(email, status, cfg):
    """Apply cooldown/disable based on upstream status.
    Returns (out_status, retry_seconds):
      - out_status: status to send to client (403 -> 429 so downstream retries)
      - retry_seconds: Retry-After hint, or None
    403 (plan limit) -> disable account, re-enabled by refresh-usage.
    """
    if not email:
        return status, None

    if status == 429:
        state = get_account_state(email)
        state.set_cooldown(COOLDOWN_429)
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {email} cooled down {COOLDOWN_429}s (429)")
        return 429, COOLDOWN_429

    if 500 <= status < 600:
        state = get_account_state(email)
        state.set_cooldown(COOLDOWN_5XX)
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {email} cooled down {COOLDOWN_5XX}s (5xx)")
        return 429, COOLDOWN_5XX

    if status in (401, 402, 403):
        if email in cfg.get("accounts", {}):
            cfg["accounts"][email]["disabled"] = True
            save_config(cfg)
        retry = next_available_in(cfg)
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {email} disabled (status {status}), retry in {retry}s")
        return 429, retry

    return status, None


def next_available_in(cfg, now=None):
    """Seconds until the next cooled-down account becomes available."""
    if now is None:
        now = time.time()
    soonest = None
    for email in cfg.get("accounts", {}):
        if cfg["accounts"][email].get("disabled"):
            continue
        state = get_account_state(email)
        rem = state.cooldown_remaining(now)
        if rem > 0:
            if soonest is None or rem < soonest:
                soonest = rem
    if soonest is None:
        return DEFAULT_RETRY_AFTER
    return int(soonest) + 1


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

        email, acct, token = self._select_request_account()
        if not token:
            ts = time.strftime("%H:%M:%S")
            retry = next_available_in(self.__class__.config)
            print(f"[{ts}] No available account (strategy={self.__class__.config.get('strategy', '?')}), retry in {retry}s")
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", str(retry))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": {
                    "code": "account_unavailable",
                    "message": "All accounts are cooling down, retry later",
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

            set_auth = resp.getheader("set-auth-token")
            if set_auth:
                ts = time.strftime("%H:%M:%S")
                print(f"[{ts}] Token rotated for {email}")
                cfg = self.__class__.config
                if email in cfg.get("accounts", {}):
                    cfg["accounts"][email]["token"] = set_auth
                    save_config(cfg)

            status = resp.status
            out_status, retry_secs = handle_upstream_status(email, status, self.__class__.config)
            rewritten = out_status != status

            if rewritten:
                resp.read()
                body_out = json.dumps({
                    "error": {
                        "message": "Account rate-limited or disabled, please retry",
                        "type": "account_unavailable",
                        "code": "account_unavailable",
                    }
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(out_status)
                self.send_header("Content-Type", "application/json")
                if retry_secs:
                    self.send_header("Retry-After", str(retry_secs))
                self._cors_headers()
                self.send_header("Content-Length", str(len(body_out)))
                self.end_headers()
                try:
                    self.wfile.write(body_out)
                except BrokenPipeError:
                    pass
                conn.close()
                call_log.record(req_model if is_chat else "unknown", email, 0, 0, "fail")
                return

            self.send_response(resp.status)
            skip = {"set-auth-token", "connection", "keep-alive", "transfer-encoding"}
            has_retry_after = False
            for key, val in resp.getheaders():
                if key.lower() not in skip:
                    self.send_header(key, val)
                    if key.lower() == "retry-after":
                        has_retry_after = True
            if status == 429 and not has_retry_after and retry_secs:
                self.send_header("Retry-After", str(retry_secs))
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
                        model = d.get("model", model)
                        u = d.get("usage", {})
                        i_tokens = u.get("prompt_tokens", 0)
                        o_tokens = u.get("completion_tokens", 0)
                        tokens_used = u.get("total_tokens", 0)
                    except Exception:
                        pass

            conn.close()

            if tokens_used > 0:
                call_log.record(model, email, i_tokens, o_tokens)
            else:
                call_log.record(req_model if is_chat else "unknown", email, 0, 0, "fail")

        except Exception as e:
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
            call_log.record(req_model if is_chat else "unknown", email, 0, 0, "error")



def main():
    parser = argparse.ArgumentParser(description="stagewise Reverse Proxy")
    parser.add_argument("--port", "-p", type=int, default=DEFAULT_PORT, help=f"Proxy port (default: {DEFAULT_PORT})")
    parser.add_argument("--strategy", type=str,
                        choices=[STRATEGY_SPECIFIC, STRATEGY_FILL_FIRST],
                        help=f"Account selection strategy (default: {STRATEGY_SPECIFIC})")
    args = parser.parse_args()

    cfg = load_config()

    if args.strategy:
        cfg["strategy"] = args.strategy
        save_config(cfg)

    token = get_active_token(cfg)
    if not token:
        if cfg.get("accounts"):
            print("! No active account set. Use activeAccount in config or switch via WebUI.")
        else:
            print("! No accounts found. Add accounts via the WebUI or directly in config.")
        if not cfg.get("accounts"):
            print("  Continuing without accounts. Requests will return 429.")

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
