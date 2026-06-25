#!/usr/bin/env python3
"""
stagewise WebUI - Full-flow management interface

Features:
  - Dashboard: session status, usage, machine ID
  - Login: email OTP authentication
  - Decrypt: DPAPI session decryption
  - API Explorer: test all endpoints
  - Proxy: start/stop reverse proxy
  - Machine ID: view / reset / spoof

Usage:
  python webui.py [--port 8080]
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.client import HTTPSConnection
from pathlib import Path

from flask import Flask, render_template, request, jsonify, Response
import key_manager
import health_tracker
import usage_store
from proxy import (
    STRATEGY_SPECIFIC, STRATEGY_FILL_FIRST, STRATEGY_ROUND_ROBIN,
    select_account, get_account_state, AccountState,
)

app = Flask(__name__)

API_HOST = "api.stagewise.io"
BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "data"
CONFIG_PATH = CONFIG_DIR / "config.json"
APPDATA = os.environ.get("APPDATA", "")

proxy_state = {"running": False, "port": 11434, "thread": None, "server": None}
refresh_state = {"running": False, "thread": None}


def load_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
        if "token" in cfg and "accounts" not in cfg:
            email = (cfg.get("user") or {}).get("email", "default")
            cfg["accounts"] = {email: {"token": cfg["token"], "user": cfg.get("user")}}
            cfg["activeAccount"] = email
            save_config(cfg)
        if "accounts" not in cfg:
            cfg["accounts"] = {}
        cfg.setdefault("strategy", STRATEGY_SPECIFIC)
        return cfg
    except Exception:
        return {"accounts": {}, "activeAccount": None, "port": 11434, "strategy": STRATEGY_SPECIFIC}


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in cfg.items() if k not in ("token", "user")}
    CONFIG_PATH.write_text(json.dumps(clean, indent=2, ensure_ascii=False), "utf-8")


def get_active_token(cfg=None):
    if cfg is None:
        cfg = load_config()
    active = cfg.get("activeAccount")
    if active and active in cfg.get("accounts", {}):
        return cfg["accounts"][active].get("token")
    return None


def get_active_user(cfg=None):
    if cfg is None:
        cfg = load_config()
    active = cfg.get("activeAccount")
    if active and active in cfg.get("accounts", {}):
        return cfg["accounts"][active].get("user")
    return None


def api_request(method, path, token=None, body=None, timeout=15):
    conn = HTTPSConnection(API_HOST, timeout=timeout)
    headers = {
        "Accept": "application/json",
        "X-Stagewise-Client": "electron/1.10.2",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req_body = None
    if body is not None:
        req_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(req_body))
    try:
        conn.request(method, path, body=req_body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        j = None
        try:
            j = json.loads(raw)
        except Exception:
            pass
        conn.close()
        return {"status": resp.status, "headers": resp_headers, "body": raw, "json": j}
    except Exception as e:
        return {"status": 0, "headers": {}, "body": str(e), "json": None}


def start_token_refresh():
    if refresh_state["running"]:
        return
    refresh_state["running"] = True

    def worker():
        while refresh_state["running"]:
            time.sleep(300)
            try:
                cfg = load_config()
                changed = False
                lock = threading.Lock()

                def refresh_account(email, acct):
                    nonlocal changed
                    token = acct.get("token")
                    if not token:
                        return
                    res = api_request("GET", "/v1/auth/get-session", token)
                    new_token = res["headers"].get("set-auth-token")
                    with lock:
                        if new_token:
                            cfg["accounts"][email]["token"] = new_token
                            changed = True
                        if res["status"] in (401, 403):
                            if cfg.get("activeAccount") == email:
                                cfg["activeAccount"] = next(
                                    (k for k in cfg["accounts"] if k != email), None
                                ) if len(cfg["accounts"]) > 1 else None
                            cfg["accounts"].pop(email, None)
                            changed = True

                accounts = list(cfg.get("accounts", {}).items())
                if accounts:
                    with ThreadPoolExecutor(max_workers=len(accounts)) as pool:
                        pool.map(lambda x: refresh_account(*x), accounts)

                if changed:
                    save_config(cfg)
            except Exception:
                pass

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    refresh_state["thread"] = t


# ─── Health / Dashboard ────────────────────────────────────────

@app.route("/api/health/availability")
def api_health_availability():
    data = health_tracker.get_availability()
    return jsonify({
        "slots": data,
        "summary": health_tracker.get_summary(),
    })


@app.route("/api/dashboard")
def api_dashboard():
    cfg = load_config()
    active = cfg.get("activeAccount")
    token = get_active_token(cfg)
    strategy = cfg.get("strategy", STRATEGY_SPECIFIC)

    accounts = list(cfg.get("accounts", {}).items())
    total_accounts = len(accounts)
    account_details = []
    detail_lock = threading.Lock()

    def fetch_account_usage(email, acct):
        state = get_account_state(email)
        is_banned = state.banned
        info = {"email": email, "banned": is_banned, "quota": None, "cooldown": False, "blocked": False}
        quota_exhausted = False
        if not is_banned:
            acct_token = acct.get("token")
            if acct_token:
                usage_res = api_request("GET", "/v1/usage/current", acct_token, timeout=5)
                if usage_res["status"] == 200 and usage_res.get("json"):
                    windows = usage_res["json"].get("windows", [])
                    info["quota"] = windows
                    for w in windows:
                        if w.get("usedPercent", 0) >= 100:
                            quota_exhausted = True
        with detail_lock:
            if is_banned:
                info["blocked"] = True
            elif quota_exhausted or not state.is_available():
                info["cooldown"] = True
            account_details.append(info)

    with ThreadPoolExecutor(max_workers=16) as pool:
        pool.map(lambda x: fetch_account_usage(*x), accounts)

    available_count = sum(1 for d in account_details if not d["cooldown"] and not d["blocked"])
    cooldown_count = sum(1 for d in account_details if d["cooldown"])
    blocked_count = sum(1 for d in account_details if d["blocked"])

    token_usage = {}
    if token:
        usage_res = api_request("GET", "/v1/usage/current", token, timeout=5)
        if usage_res["status"] == 200 and usage_res.get("json"):
            windows = usage_res["json"].get("windows", [])
            for w in windows:
                token_usage[w.get("type", "")] = {
                    "used": w.get("used", 0),
                    "limit": w.get("limit", 0),
                    "percent": w.get("usedPercent", 0),
                }

    granularity = request.args.get("granularity", "daily")
    if granularity == "hourly":
        token_history = usage_store.get_hourly(hours=24)
    elif granularity == "weekly":
        token_history = usage_store.get_weekly(weeks=12)
    else:
        token_history = usage_store.get_daily(days=30)

    return jsonify({
        "tokenUsage": token_usage,
        "tokenHistory": token_history,
        "availability": health_tracker.get_availability(),
        "availabilitySummary": health_tracker.get_summary(),
        "proxy": {
            "running": proxy_state["running"],
            "port": proxy_state["port"],
            "strategy": strategy,
        },
        "accounts": {
            "total": total_accounts,
            "available": available_count,
            "cooldown": cooldown_count,
            "blocked": blocked_count,
            "details": account_details,
        },
        "activeAccount": active,
    })


# ─── Strategy ───────────────────────────────────────────────────

@app.route("/api/strategy", methods=["GET"])
def api_strategy_get():
    cfg = load_config()
    return jsonify({
        "strategy": cfg.get("strategy", STRATEGY_SPECIFIC),
        "activeAccount": cfg.get("activeAccount"),
        "accountCount": len(cfg.get("accounts", {})),
    })


@app.route("/api/strategy", methods=["POST"])
def api_strategy_set():
    cfg = load_config()
    strategy = request.json.get("strategy", "").strip()
    if strategy not in (STRATEGY_SPECIFIC, STRATEGY_FILL_FIRST, STRATEGY_ROUND_ROBIN):
        return jsonify({"error": f"Invalid strategy. Choose from: {STRATEGY_SPECIFIC}, {STRATEGY_FILL_FIRST}, {STRATEGY_ROUND_ROBIN}"}), 400
    cfg["strategy"] = strategy
    save_config(cfg)
    return jsonify({"success": True, "strategy": strategy})


# ─── Routes ────────────────────────────────────────────────────

@app.route("/")
@app.route("/dashboard")
@app.route("/login")
@app.route("/accounts")
@app.route("/chat")
@app.route("/api-explorer")
@app.route("/proxy")
@app.route("/apikeys")
def index():
    tab_map = {
        "dashboard": "dashboard",
        "login": "login",
        "accounts": "accounts",
        "chat": "chat",
        "decrypt": "decrypt",
        "api-explorer": "api",
        "proxy": "proxy",
        "apikeys": "apikeys",
        "machineid": "machineid",
    }
    path = request.path.strip("/") or "dashboard"
    default_tab = tab_map.get(path, "dashboard")
    return render_template("index.html", default_tab=default_tab)


@app.route("/api/status")
def api_status():
    cfg = load_config()
    token = get_active_token(cfg)
    user = get_active_user(cfg)
    active = cfg.get("activeAccount")
    account_count = len(cfg.get("accounts", {}))
    result = {
        "hasToken": bool(token),
        "tokenPreview": token[:20] + "..." if token else None,
        "user": user,
        "activeAccount": active,
        "accountCount": account_count,
        "strategy": cfg.get("strategy", STRATEGY_SPECIFIC),
        "proxyRunning": proxy_state["running"],
        "proxyPort": proxy_state["port"],
    }
    if token:
        res = api_request("GET", "/v1/auth/get-session", token)
        result["sessionValid"] = res["status"] == 200
        if res["status"] == 200 and res.get("json"):
            u = res["json"].get("user") or {}
            result["sessionUser"] = u.get("email") or (user or {}).get("email")
            result["sessionExpiresAt"] = (res["json"].get("session") or {}).get("expiresAt")
        new_token = res["headers"].get("set-auth-token")
        if new_token and active:
            cfg["accounts"][active]["token"] = new_token
            save_config(cfg)
            result["tokenRotated"] = True
    return jsonify(result)


@app.route("/api/usage")
def api_usage():
    cfg = load_config()
    token = get_active_token(cfg)
    if not token:
        return jsonify({"error": "No active account"}), 401
    res = api_request("GET", "/v1/usage/current", token)
    return jsonify(res.get("json") or {"error": res["body"]}), res["status"] if res["status"] else 500


@app.route("/api/subscription")
def api_subscription():
    cfg = load_config()
    token = get_active_token(cfg)
    if not token:
        return jsonify({"error": "No active account"}), 401
    res = api_request("GET", "/v1/billing/plan", token)
    return jsonify(res.get("json") or {"error": res["body"]}), res["status"] if res["status"] else 500


@app.route("/api/usage-history")
def api_usage_history():
    cfg = load_config()
    token = get_active_token(cfg)
    days = request.args.get("days", "7")
    if not token:
        return jsonify({"error": "No active account"}), 401
    res = api_request("GET", f"/v1/usage/history?days={days}", token)
    return jsonify(res.get("json") or {"error": res["body"]}), res["status"] if res["status"] else 500


@app.route("/api/send-otp", methods=["POST"])
def api_send_otp():
    email = request.json.get("email", "").strip()
    if not email or "@" not in email:
        return jsonify({"error": "Invalid email"}), 400
    res = api_request(
        "POST", "/v1/auth/email-otp/send-verification-otp",
        body={"email": email, "type": "sign-in"},
    )
    if res["status"] == 200 and not (res.get("json") or {}).get("error"):
        return jsonify({"success": True, "message": "OTP sent"})
    err = (res.get("json") or {}).get("error", {})
    msg = err.get("message", err) if isinstance(err, dict) else str(err)
    return jsonify({"success": False, "error": msg}), 400


@app.route("/api/verify-otp", methods=["POST"])
def api_verify_otp():
    email = request.json.get("email", "").strip()
    otp = request.json.get("otp", "").strip()
    if not email or not otp:
        return jsonify({"error": "Email and OTP required"}), 400
    res = api_request(
        "POST", "/v1/auth/sign-in/email-otp",
        body={"email": email, "otp": otp},
    )
    set_auth = res["headers"].get("set-auth-token")
    j = res.get("json") or {}
    token = set_auth or j.get("token") or (j.get("data") or {}).get("token")
    user = j.get("user") or (j.get("data") or {}).get("user")
    if token:
        email = (user or {}).get("email", email)
        cfg = load_config()
        cfg["accounts"][email] = {"token": token, "user": user}
        cfg["activeAccount"] = email
        save_config(cfg)
        return jsonify({
            "success": True,
            "token": token[:20] + "...",
            "user": user,
            "activeAccount": email,
        })
    err_msg = (j.get("error") or {}).get("message", "Login failed")
    return jsonify({"success": False, "error": err_msg}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    cfg = load_config()
    active = cfg.get("activeAccount")
    if active and active in cfg.get("accounts", {}):
        token = cfg["accounts"][active].get("token")
        if token:
            try:
                api_request("POST", "/v1/auth/sign-out", token)
            except Exception:
                pass
        del cfg["accounts"][active]
    cfg["activeAccount"] = next(iter(cfg.get("accounts", {}))) if cfg.get("accounts") else None
    save_config(cfg)
    return jsonify({"success": True})


@app.route("/api/test-endpoint", methods=["POST"])
def api_test_endpoint():
    cfg = load_config()
    token = get_active_token(cfg)
    method = request.json.get("method", "GET")
    path = request.json.get("path", "")
    body = request.json.get("body")
    if not path:
        return jsonify({"error": "Path required"}), 400
    res = api_request(method, path, token, body if body else None, timeout=30)
    return jsonify({
        "status": res["status"],
        "body": res["body"][:2000],
        "json": res.get("json"),
    })


@app.route("/api/proxy/start", methods=["POST"])
def _start_proxy_instance(port):
    """Start the proxy HTTPServer in the current thread. Blocks until server stops."""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from urllib.parse import urlparse

    _cfg = load_config()
    _cfg["port"] = port

    class Handler(BaseHTTPRequestHandler):
        config = _cfg

        def log_message(self, format, *args):
            pass

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            self._proxy()

        def do_POST(self):
            self._proxy()

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, x-stainless-*, X-Account-Email")

        def _select_account(self):
            cfg = self.__class__.config
            strategy = cfg.get("strategy", STRATEGY_SPECIFIC)
            specific_email = None
            if strategy == STRATEGY_SPECIFIC:
                specific_email = self.headers.get("X-Account-Email") or cfg.get("activeAccount")
            email, acct = select_account(cfg, strategy, specific_email)
            if email and acct:
                return email, acct, acct.get("token")
            return None, None, None

        def _proxy(self):
            cl = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(cl) if cl > 0 else b""

            is_chat = "/chat/completions" in self.path
            api_key = key_manager.extract_key_from_header(self.headers.get("Authorization", ""))
            if is_chat or api_key:
                if not api_key:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self._cors()
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "API key required. Create one in the WebUI.", "type": "auth_error"}).encode())
                    return
                k = key_manager.validate_key(api_key)
                if k is None:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self._cors()
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Invalid or rate-limited API key", "type": "auth_error"}).encode())
                    return

            email, acct, token = self._select_account()
            if not token:
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", "30")
                self._cors()
                self.end_headers()
                self.wfile.write(json.dumps({
                    "error": {"code": "account_unavailable", "message": "All accounts are cooling down"}
                }).encode())
                return

            target = urlparse(f"https://{API_HOST}")
            conn = HTTPSConnection(target.hostname, target.port or 443, timeout=120)
            path = self.path
            if is_chat:
                path = path.replace("/v1/chat/completions", "/v1/ai/chat/completions")
                uh = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "ai-sdk/openai-compatible/2.0.47 ai-sdk/provider-utils/4.0.27 runtime/node.js/24",
                    "X-Stagewise-Client": "electron/1.11.0",
                    "Host": target.hostname,
                }
                if body:
                    try:
                        req = json.loads(body.decode("utf-8"))
                        req.setdefault("reasoning", {"enabled": True, "effort": "low"})
                        req.setdefault("provider", {"require_parameters": True})
                        has_system = any(m.get("role") == "system" for m in req.get("messages", []))
                        if not has_system:
                            try:
                                sp = Path("C:/Desktop/system_prompt.txt").read_text("utf-8")
                                req.setdefault("messages", []).insert(0, {"role": "system", "content": sp})
                            except Exception:
                                pass
                        body = json.dumps(req, ensure_ascii=False).encode("utf-8")
                    except Exception:
                        pass
                if body:
                    uh["Content-Length"] = str(len(body))
            else:
                uh = {}
                for k, v in self.headers.items():
                    if k.lower() not in ("host", "connection", "proxy-connection", "keep-alive", "transfer-encoding"):
                        uh[k] = v
                uh["Authorization"] = f"Bearer {token}"
                uh["Host"] = target.hostname
                if body:
                    uh["Content-Length"] = str(len(body))
                    if "Content-Type" not in uh:
                        uh["Content-Type"] = "application/json"
            try:
                conn.request(self.command, path, body=body, headers=uh)
                resp = conn.getresponse()
                health_tracker.record_request(success=200 <= resp.status < 400)
                set_auth = resp.getheader("set-auth-token")
                if set_auth and email:
                    cfg = self.__class__.config
                    if email in cfg.get("accounts", {}):
                        cfg["accounts"][email]["token"] = set_auth
                        save_config(cfg)
                status = resp.status
                if email and (status in (401, 403, 429) or (500 <= status < 600)):
                    get_account_state(email).apply_cooldown(status)
                self.send_response(resp.status)
                skip = {"connection", "keep-alive", "transfer-encoding", "set-auth-token"}
                for k, v in resp.getheaders():
                    if k.lower() not in skip:
                        self.send_header(k, v)
                self._cors()
                self.end_headers()
                ct = resp.getheader("content-type", "")
                tokens_used = 0
                if "text/event-stream" in ct:
                    last_chunk = None
                    while True:
                        chunk = resp.read(4096)
                        if not chunk:
                            break
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            last_chunk = chunk
                        except BrokenPipeError:
                            break
                    if api_key and last_chunk:
                        for line in last_chunk.decode("utf-8", errors="replace").split("\n"):
                            if line.startswith("data: ") and "[DONE]" not in line:
                                try:
                                    d = json.loads(line[6:])
                                    tokens_used = d.get("usage", {}).get("total_tokens", 0)
                                except Exception:
                                    pass
                else:
                    data = resp.read()
                    self.wfile.write(data)
                    if api_key and resp.status == 200:
                        try:
                            d = json.loads(data.decode("utf-8", errors="replace"))
                            tokens_used = d.get("usage", {}).get("total_tokens", 0)
                        except Exception:
                            pass
                conn.close()
                if tokens_used > 0:
                    usage_store.record_usage(tokens_used)
                if api_key and tokens_used > 0:
                    key_manager.record_usage(api_key, tokens_used)
                elif api_key:
                    key_manager.record_usage(api_key, 0, 1)
            except Exception as e:
                health_tracker.record_request(success=False)
                if not self.headers_sent:
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self._cors()
                    self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())

    try:
        server = HTTPServer(("0.0.0.0", port), Handler)
        proxy_state["server"] = server
        server.serve_forever()
    except Exception:
        proxy_state["running"] = False


def api_proxy_start():
    if proxy_state["running"]:
        return jsonify({"error": "Proxy already running"}), 400
    cfg = load_config()
    if not cfg.get("accounts"):
        return jsonify({"error": "No accounts. Login or add an account first."}), 401
    port = request.json.get("port", 11434) if request.json else 11434
    proxy_state["port"] = port
    proxy_state["running"] = True

    t = threading.Thread(target=lambda: _start_proxy_instance(port), daemon=True)
    t.start()
    proxy_state["thread"] = t
    return jsonify({"success": True, "port": port})


@app.route("/api/proxy/stop", methods=["POST"])
def api_proxy_stop():
    if not proxy_state["running"]:
        return jsonify({"error": "Proxy not running"}), 400
    if proxy_state["server"]:
        threading.Thread(target=proxy_state["server"].shutdown, daemon=True).start()
    proxy_state["running"] = False
    proxy_state["server"] = None
    return jsonify({"success": True})


@app.route("/api/proxy/status")
def api_proxy_status():
    cfg = load_config()
    return jsonify({
        "running": proxy_state["running"],
        "port": proxy_state["port"],
        "strategy": cfg.get("strategy", STRATEGY_SPECIFIC),
    })


@app.route("/api/llm-test", methods=["POST"])
def api_llm_test():
    cfg = load_config()
    token = get_active_token(cfg)
    if not token:
        return jsonify({"error": "No active account"}), 401
    model = request.json.get("model", "anthropic/claude-fable-5")
    prompt = request.json.get("prompt", "Say hello in exactly 3 words.")
    res = api_request("POST", "/v1/ai/chat/completions", token, {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 50,
        "stream": False,
    }, timeout=30)
    return jsonify({
        "status": res["status"],
        "body": res["body"][:2000],
        "json": res.get("json"),
    })


@app.route("/api/accounts")
def api_accounts():
    cfg = load_config()
    active = cfg.get("activeAccount")
    entries = {email: {"email": email, "tokenPreview": None, "user": acct.get("user"), "active": email == active, "usage": None, "expiresAt": None} for email, acct in cfg.get("accounts", {}).items()}

    tasks = {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        for email, acct in cfg.get("accounts", {}).items():
            token = acct.get("token")
            if not token:
                continue
            entries[email]["tokenPreview"] = token[:20] + "..." if token else None
            tasks[pool.submit(api_request, "GET", "/v1/usage/current", token, timeout=10)] = (email, "usage")
            tasks[pool.submit(api_request, "GET", "/v1/auth/get-session", token, timeout=10)] = (email, "session")

        for f in as_completed(tasks):
            email, kind = tasks[f]
            try:
                r = f.result()
                if r["status"] != 200 or not r.get("json"):
                    continue
                if kind == "usage":
                    entries[email]["usage"] = r["json"]
                else:
                    session = r["json"].get("session") or {}
                    entries[email]["expiresAt"] = session.get("expiresAt")
            except Exception:
                pass

    return jsonify({"accounts": list(entries.values()), "activeAccount": active})


@app.route("/api/accounts/switch", methods=["POST"])
def api_accounts_switch():
    cfg = load_config()
    email = request.json.get("email", "").strip()
    if email not in cfg.get("accounts", {}):
        return jsonify({"error": "Account not found"}), 404
    cfg["activeAccount"] = email
    save_config(cfg)
    return jsonify({"success": True, "activeAccount": email})


@app.route("/api/accounts/remove", methods=["POST"])
def api_accounts_remove():
    cfg = load_config()
    email = request.json.get("email", "").strip()
    if email not in cfg.get("accounts", {}):
        return jsonify({"error": "Account not found"}), 404
    del cfg["accounts"][email]
    if cfg.get("activeAccount") == email:
        remaining = list(cfg["accounts"].keys())
        cfg["activeAccount"] = remaining[0] if remaining else None
    save_config(cfg)
    return jsonify({"success": True})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    cfg_path = Path(__file__).parent / "data" / "config.json"
    cfg = json.loads(cfg_path.read_text("utf-8"))
    active = cfg.get("activeAccount")
    token = cfg["accounts"][active]["token"] if active and active in cfg.get("accounts", {}) else None
    if not token:
        return jsonify({"error": "No active account"}), 401

    message = request.json.get("message", "")
    model = request.json.get("model", "deepseek/deepseek-v4-flash")
    stream = request.json.get("stream", True)

    system_prompt = open("C:/Desktop/system_prompt.txt", encoding="utf-8").read()
    base_payload = {
        "model": model,
        "reasoning": {"enabled": True, "effort": "low"},
        "provider": {"require_parameters": True},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [{"type": "text", "text": message}]},
        ],
    }

    import http.client as _http

    if stream:
        body_bytes = json.dumps({**base_payload, "stream": True, "stream_options": {"include_usage": True}}, ensure_ascii=False).encode("utf-8")
        conn = _http.HTTPSConnection(API_HOST, timeout=120)
        try:
            conn.request("POST", "/v1/ai/chat/completions", body=body_bytes, headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "X-Stagewise-Client": "electron/1.10.2",
                "Content-Length": str(len(body_bytes)),
            })
            resp = conn.getresponse()
        except Exception as e:
            conn.close()
            return jsonify({"error": f"Stream request failed: {e}"}), 502

        if resp.status != 200:
            err_body = resp.read().decode("utf-8", errors="replace")
            conn.close()
            try:
                err_json = json.loads(err_body)
                msg = err_json.get("error", {})
                if isinstance(msg, dict):
                    msg = msg.get("message", err_body[:300])
                return jsonify({"error": msg, "status": resp.status}), resp.status
            except Exception:
                return jsonify({"error": err_body[:300], "status": resp.status}), resp.status

        def generate():
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    yield chunk
            finally:
                conn.close()
        return Response(generate(), content_type="text/event-stream")

    body_bytes = json.dumps(base_payload, ensure_ascii=False).encode("utf-8")
    conn = _http.HTTPSConnection(API_HOST, timeout=120)
    try:
        conn.request("POST", "/v1/ai/chat/completions", body=body_bytes, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Stagewise-Client": "electron/1.10.2",
            "Content-Length": str(len(body_bytes)),
        })
        resp = conn.getresponse()
    except Exception as e:
        conn.close()
        return jsonify({"error": f"Request failed: {e}"}), 502

    if resp.status != 200:
        err_body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        try:
            err_json = json.loads(err_body)
            msg = err_json.get("error", {})
            if isinstance(msg, dict):
                msg = msg.get("message", err_body[:300])
            return jsonify({"error": msg, "status": resp.status}), resp.status
        except Exception:
            return jsonify({"error": err_body[:300], "status": resp.status}), resp.status

    try:
        data = json.loads(resp.read().decode("utf-8"))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)})
    finally:
        conn.close()


@app.route("/api/keys")
def api_keys():
    return jsonify({"keys": key_manager.list_keys()})


@app.route("/api/keys/create", methods=["POST"])
def api_keys_create():
    name = request.json.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    monthly_tokens = request.json.get("monthly_tokens")
    monthly_requests = request.json.get("monthly_requests")
    key, key_id = key_manager.create_key(name, monthly_tokens, monthly_requests)
    return jsonify({
        "success": True, "key": key, "key_id": key_id, "keyPreview": key[:20] + "...", "name": name,
        "monthly_tokens": monthly_tokens or key_manager.DEFAULT_MONTHLY_TOKENS,
        "monthly_requests": monthly_requests or key_manager.DEFAULT_MONTHLY_REQUESTS,
    })


@app.route("/api/keys/delete", methods=["POST"])
def api_keys_delete():
    key_id = request.json.get("key_id", "").strip()
    if not key_id:
        return jsonify({"error": "key_id required"}), 400
    if key_manager.delete_key(key_id):
        return jsonify({"success": True})
    return jsonify({"error": "Key not found"}), 404


@app.route("/api/keys/toggle", methods=["POST"])
def api_keys_toggle():
    key_id = request.json.get("key_id", "").strip()
    if not key_id:
        return jsonify({"error": "key_id required"}), 400
    if key_manager.toggle_key(key_id):
        return jsonify({"success": True})
    return jsonify({"error": "Key not found"}), 404


def main():
    parser = argparse.ArgumentParser(description="stagewise WebUI")
    parser.add_argument("--port", "-p", type=int, default=8080, help="WebUI port (default: 8080)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()

    if args.debug:
        print("WARNING: Debug mode enables reloader. Token refresh runs separately.")
    start_token_refresh()

    # Auto-start proxy
    cfg = load_config()
    proxy_port = cfg.get("port", 11434)
    if cfg.get("accounts"):
        import threading, time
        def auto_start():
            time.sleep(0.5)
            from http.server import HTTPServer
            from http.server import BaseHTTPRequestHandler
            _start_proxy_instance(proxy_port)
        t = threading.Thread(target=auto_start, daemon=True)
        t.start()
    else:
        print("-> No accounts. Proxy not started. Login at /login to add accounts.")

    print()
    print("=" * 50)
    print("   stagewise WebUI - Multi-Account")
    print(f"   http://localhost:{args.port}")
    print(f"   Proxy:    http://localhost:{proxy_port}/v1")
    print("=" * 50)
    print()
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
