#!/usr/bin/env python3
"""
stagewise WebUI - Multi-Account Management Interface

Features:
  - Dashboard: account status, usage, call log
  - Accounts: manage multi-account pool, add/remove/switch
  - Proxy: start/stop reverse proxy
  - Chat: LLM chat via proxy
  - API Explorer: test all endpoints
  - Proxy: manage local access keys

Usage:
  python webui.py [--port 8080]
"""

import argparse
import base64
import json
import os
import requests
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.client import HTTPSConnection
from pathlib import Path

from flask import Flask, render_template, request, jsonify, Response
import call_log
from proxy import (
    STRATEGY_SPECIFIC, STRATEGY_FILL_FIRST,
    select_account, get_account_state, AccountState,
    handle_upstream_status, next_available_in, resolve_model,
)

app = Flask(__name__)

API_HOST = "api.stagewise.io"
BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "data"
CONFIG_PATH = CONFIG_DIR / "config.json"
APPDATA = os.environ.get("APPDATA", "")

SYSTEM_PROMPT = "The following sections define your identity and operating environment:- `<soul>` — Identity, behavior rules, and values- `<environment>` — Tools, interfaces, file system, and skill system- `<output-style>` — Response formatting and special protocols- `<authorities>` — Trust hierarchy and security model### Priority Hierarchy1. **`plugins/{id}/SKILL.md`** — Core intrinsic knowledge. Always prefer.2. **`globalskills-sw/*`** — User-level skills from `~/.stagewise/skills/`. Personal defaults across all workspaces.3. **`{WORKSPACE}/.stagewise/skills/*`** — Workspace-specific, created for you. Overrides general skills.4. **`globalskills-agents/*`** — Cross-agent user-level skills from `~/.agents/skills/`.5. **`{WORKSPACE}/.agents/skills/*`** — General skills shared with other agents.## AGENTS.md (Legacy)Inside a workspace, an `AGENTS.md` file at the workspace root may carry legacy project documentation written for previous coding agents. **Ignore this file unless you already have it loaded in your context** — the canonical project memo lives at `.stagewise/WORKSPACE.md` (see the WORKSPACE.md section below). Never read `AGENTS.md` proactively to warm up on a project; rely on `<agents-md>` entries that already surface it."

proxy_state = {"running": False, "port": 11434, "thread": None, "server": None}


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


# ─── Health / Dashboard ────────────────────────────────────────

@app.route("/api/dashboard")
def api_dashboard():
    cfg = load_config()
    active = cfg.get("activeAccount")
    strategy = cfg.get("strategy", STRATEGY_SPECIFIC)

    accounts = list(cfg.get("accounts", {}).items())
    total_accounts = len(accounts)
    account_details = []

    for email, acct in accounts:
        is_disabled = acct.get("disabled", False)
        if is_disabled:
            account_details.append({"email": email, "disabled": True, "cooling": False, "cooldown_remaining": 0, "blocked": False, "quota": None})
        else:
            state = get_account_state(email)
            rem = int(state.cooldown_remaining()) if state else 0
            account_details.append({"email": email, "disabled": False, "cooling": rem > 0, "cooldown_remaining": rem, "blocked": False, "quota": None})

    available_count = sum(1 for d in account_details if not d.get("disabled") and not d.get("cooling"))
    cooling_count = sum(1 for d in account_details if d.get("cooling"))
    disabled_count = sum(1 for d in account_details if d.get("disabled"))

    return jsonify({
        "proxy": {
            "running": proxy_state["running"],
            "port": proxy_state["port"],
            "strategy": strategy,
        },
        "accounts": {
            "total": total_accounts,
            "available": available_count,
            "cooling": cooling_count,
            "disabled": disabled_count,
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
    if strategy not in (STRATEGY_SPECIFIC, STRATEGY_FILL_FIRST):
        return jsonify({"error": f"Invalid strategy. Choose from: {STRATEGY_SPECIFIC}, {STRATEGY_FILL_FIRST}"}), 400
    cfg["strategy"] = strategy
    save_config(cfg)
    return jsonify({"success": True, "strategy": strategy})


# ─── Routes ────────────────────────────────────────────────────

@app.route("/")
@app.route("/dashboard")
@app.route("/accounts")
@app.route("/chat")
@app.route("/api-explorer")
@app.route("/proxy")
def index():
    tab_map = {
        "dashboard": "dashboard",
        "accounts": "accounts",
        "chat": "chat",
        "decrypt": "decrypt",
        "api-explorer": "api",
        "proxy": "proxy",
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
    return jsonify({
        "hasToken": bool(token),
        "tokenPreview": token[:20] + "..." if token else None,
        "user": user,
        "activeAccount": active,
        "accountCount": account_count,
        "strategy": cfg.get("strategy", STRATEGY_SPECIFIC),
        "proxyRunning": proxy_state["running"],
        "proxyPort": proxy_state["port"],
    })


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


def _start_proxy_instance(port):
    """Start the proxy HTTPServer in the current thread. Blocks until server stops."""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from urllib.parse import urlparse

    proxy_state["running"] = True
    proxy_state["port"] = port

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            if self.path in ("/v1/models", "/models"):
                self._handle_models()
                return
            self._proxy()

        def do_POST(self):
            self._proxy()

        def _handle_models(self):
            models = [
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
            resp = {"object": "list", "data": models}
            body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, x-stainless-*, X-Account-Email")

        def _select_account(self):
            cfg = load_config()
            strategy = cfg.get("strategy", STRATEGY_SPECIFIC)
            specific_email = None
            if strategy == STRATEGY_SPECIFIC:
                specific_email = self.headers.get("X-Account-Email") or cfg.get("activeAccount")
            email, acct = select_account(cfg, strategy, specific_email)
            if email and acct:
                return email, acct, acct.get("token")
            return None, None, None

        def _proxy(self):
            self._responded = False
            cl = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(cl) if cl > 0 else b""

            is_chat = "/chat/completions" in self.path

            email, acct, token = self._select_account()
            if not token:
                retry = next_available_in(load_config())
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", str(retry))
                self._cors()
                self.end_headers()
                self.wfile.write(json.dumps({
                    "error": {"code": "account_unavailable", "message": "All accounts are cooling down, retry later"}
                }).encode())
                return

            target = urlparse(f"https://{API_HOST}")
            conn = HTTPSConnection(target.hostname, target.port or 443, timeout=120)
            path = self.path
            req_model = "unknown"
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
                        req_model = req.get("model", "unknown")
                        req["model"] = resolve_model(req["model"])
                        req.setdefault("reasoning", {"enabled": True, "effort": "low"})
                        req.setdefault("provider", {"require_parameters": True})
                        req.setdefault("messages", []).insert(0, {"role": "system", "content": SYSTEM_PROMPT})
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
                cur_cfg = load_config()
                set_auth = resp.getheader("set-auth-token")
                if set_auth and email:
                    if email in cur_cfg.get("accounts", {}):
                        cur_cfg["accounts"][email]["token"] = set_auth
                        save_config(cur_cfg)
                status = resp.status
                out_status, retry_secs = handle_upstream_status(email, status, cur_cfg)
                rewritten = out_status != status

                if rewritten:
                    resp.read()
                    body_out = json.dumps({
                        "error": {
                            "message": "Account rate-limited or disabled, please retry",
                            "type": "account_unavailable",
                            "code": "account_unavailable",
                        }
                    }, ensure_ascii=False).encode()
                    self.send_response(out_status)
                    self.send_header("Content-Type", "application/json")
                    if retry_secs:
                        self.send_header("Retry-After", str(retry_secs))
                    self._cors()
                    self.send_header("Content-Length", str(len(body_out)))
                    self.end_headers()
                    self._responded = True
                    try:
                        self.wfile.write(body_out)
                    except BrokenPipeError:
                        pass
                    conn.close()
                    call_log.record(req_model if is_chat else "unknown", email, 0, 0, "fail")
                    return

                self.send_response(resp.status)
                skip = {"connection", "keep-alive", "transfer-encoding", "set-auth-token"}
                has_retry_after = False
                for k, v in resp.getheaders():
                    if k.lower() not in skip:
                        self.send_header(k, v)
                        if k.lower() == "retry-after":
                            has_retry_after = True
                if status == 429 and not has_retry_after and retry_secs:
                    self.send_header("Retry-After", str(retry_secs))
                self._cors()
                self.end_headers()
                self._responded = True
                ct = resp.getheader("content-type", "")
                tokens_used = 0
                model = "unknown"
                i_tokens = 0
                o_tokens = 0
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
                    if last_chunk:
                        for line in last_chunk.decode("utf-8", errors="replace").split("\n"):
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
                    call_log.record(req_model if is_chat else model, email, 0, 0, "fail")
            except Exception as e:
                if not self._responded:
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self._cors()
                    self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
                call_log.record(req_model if is_chat else "unknown", email, 0, 0, "error")

    try:
        server = HTTPServer(("0.0.0.0", port), Handler)
        proxy_state["server"] = server
        server.serve_forever()
    except Exception:
        proxy_state["running"] = False


@app.route("/api/proxy/start", methods=["POST"])
def api_proxy_start():
    if proxy_state["running"]:
        return jsonify({"error": "Proxy already running"}), 400
    cfg = load_config()
    if not cfg.get("accounts"):
        return jsonify({"error": "No accounts. Add an account first."}), 401
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
    page = max(1, request.args.get("page", 1, type=int))
    per_page = max(1, request.args.get("per_page", 0, type=int))
    refresh = request.args.get("refresh", 0, type=int) == 1
    all_emails = list(cfg.get("accounts", {}).keys())
    total = len(all_emails)
    if per_page > 0:
        start = (page - 1) * per_page
        page_emails = set(all_emails[start:start + per_page])
    else:
        page_emails = set(all_emails)
    accounts = cfg.get("accounts", {})
    entries = {email: {"email": email, "tokenPreview": None, "user": acct.get("user"), "active": email == active, "disabled": acct.get("disabled", False), "usage": None, "expiresAt": None} for email, acct in accounts.items()}

    tasks = {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        for email, acct in accounts.items():
            token = acct.get("token")
            if not token:
                continue
            entries[email]["tokenPreview"] = token[:20] + "..." if token else None
            if email not in page_emails:
                continue
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

    enabled_count = 0
    if refresh:
        for email in page_emails:
            acct = accounts.get(email)
            if not acct or not acct.get("disabled"):
                continue
            usage = entries[email].get("usage")
            if not usage:
                continue
            windows = usage.get("windows", [])
            if windows and all(w.get("usedPercent", 0) < 100 for w in windows):
                cfg["accounts"][email]["disabled"] = False
                entries[email]["disabled"] = False
                enabled_count += 1
        if enabled_count > 0:
            save_config(cfg)

    return jsonify({"accounts": list(entries.values()), "activeAccount": active, "total": total, "page": page, "per_page": per_page, "enabled": enabled_count})


@app.route("/api/accounts/switch", methods=["POST"])
def api_accounts_switch():
    cfg = load_config()
    email = request.json.get("email", "").strip()
    if email not in cfg.get("accounts", {}):
        return jsonify({"error": "Account not found"}), 404
    cfg["activeAccount"] = email
    save_config(cfg)
    return jsonify({"success": True, "activeAccount": email})


@app.route("/api/accounts/disable", methods=["POST"])
def api_accounts_disable():
    cfg = load_config()
    email = request.json.get("email", "").strip()
    if email not in cfg.get("accounts", {}):
        return jsonify({"error": "Account not found"}), 404
    cfg["accounts"][email]["disabled"] = True
    save_config(cfg)
    return jsonify({"success": True, "email": email, "disabled": True})


@app.route("/api/accounts/refresh-usage", methods=["POST"])
def api_accounts_refresh_usage():
    cfg = load_config()
    accounts = cfg.get("accounts", {})
    if not accounts:
        return jsonify({"success": True, "enabled": 0, "total": 0})

    enabled_count = 0
    lock = threading.Lock()

    def check_and_enable(email, acct):
        nonlocal enabled_count
        if not acct.get("disabled"):
            return
        token = acct.get("token")
        if not token:
            return
        usage_res = api_request("GET", "/v1/usage/current", token, timeout=10)
        if usage_res["status"] == 200 and usage_res.get("json"):
            windows = usage_res["json"].get("windows", [])
            all_clear = all(w.get("usedPercent", 0) < 100 for w in windows)
            if all_clear:
                with lock:
                    cfg["accounts"][email]["disabled"] = False
                    enabled_count += 1

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda x: check_and_enable(*x), accounts.items()))

    if enabled_count > 0:
        save_config(cfg)

    return jsonify({"success": True, "enabled": enabled_count, "total": len(accounts)})

@app.route("/api/accounts/enable", methods=["POST"])
def api_accounts_enable():
    cfg = load_config()
    email = request.json.get("email", "").strip()
    if email not in cfg.get("accounts", {}):
        return jsonify({"error": "Account not found"}), 404
    cfg["accounts"][email]["disabled"] = False
    save_config(cfg)
    return jsonify({"success": True, "email": email, "disabled": False})


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


@app.route("/api/accounts/add", methods=["POST"])
def api_accounts_add():
    cfg = load_config()
    data = request.json
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid request body"}), 400
    email = data.get("email")
    token = data.get("token")
    if not isinstance(email, str) or not isinstance(token, str):
        return jsonify({"error": "email and token must be strings"}), 400
    email = email.strip()
    token = token.strip()
    if not email or "@" not in email or email.startswith("@") or email.endswith("@"):
        return jsonify({"error": "Valid email required"}), 400
    if not token:
        return jsonify({"error": "Token required"}), 400

    accounts = cfg.get("accounts", {})
    status = "added"
    if email in accounts:
        if accounts[email].get("token") == token:
            return jsonify({"success": True, "email": email, "activeAccount": cfg.get("activeAccount"), "status": "exists", "index": list(accounts.keys()).index(email)})
        accounts[email]["token"] = token
        status = "overwritten"
    else:
        accounts[email] = {"token": token, "user": None}
    cfg["accounts"] = accounts
    if not cfg.get("activeAccount"):
        cfg["activeAccount"] = email
    save_config(cfg)
    emails = list(accounts.keys())
    index = emails.index(email)
    return jsonify({"success": True, "email": email, "activeAccount": cfg["activeAccount"], "status": status, "index": index})


@app.route("/api/accounts/add-batch", methods=["POST"])
def api_accounts_add_batch():
    cfg = load_config()
    existing = cfg.get("accounts", {})
    data = request.json or {}

    raw_items = []  # (source_label, line_no, email_raw, token_raw)
    errors = []     # {line, source, reason}

    json_list = data.get("accounts", [])
    if not isinstance(json_list, list):
        return jsonify({"error": "`accounts` must be a JSON array."}), 400
    for i, acct in enumerate(json_list):
        if not isinstance(acct, dict):
            errors.append({"line": i + 1, "source": "json", "reason": "Each item must be a JSON object"})
            continue
        email = acct.get("email")
        token = acct.get("token")
        if not isinstance(email, str) or not isinstance(token, str):
            errors.append({"line": i + 1, "source": "json", "reason": "email/token must be strings"})
            continue
        raw_items.append(("json", i + 1, email.strip(), token.strip()))

    batch_text = data.get("batch_text", "")
    if not isinstance(batch_text, str):
        batch_text = ""
    if batch_text.strip():
        for ln, line in enumerate(batch_text.split("\n"), 1):
            s = line.strip()
            if not s:
                continue
            parts = s.split("|", 1)
            if len(parts) != 2:
                errors.append({"line": ln, "source": "text", "reason": "Missing '|' separator"})
                continue
            email, token = parts[0].strip(), parts[1].strip()
            if not email:
                errors.append({"line": ln, "source": "text", "reason": "Empty email"})
                continue
            if not token:
                errors.append({"line": ln, "source": "text", "reason": "Empty token"})
                continue
            if "@" not in email:
                errors.append({"line": ln, "source": "text", "reason": "Invalid email (missing @)"})
                continue
            raw_items.append(("text", ln, email, token))

    if not raw_items:
        return jsonify({"error": "No valid accounts provided. Use JSON array or batch_text format.", "errors": errors}), 400

    added = 0
    overwritten = []
    skipped_dup = []
    seen_in_batch = set()

    for source, line_no, email, token in raw_items:
        if email in seen_in_batch:
            skipped_dup.append({"line": line_no, "source": source, "email": email, "reason": "Duplicate within this import"})
            continue
        seen_in_batch.add(email)
        if email in existing:
            if existing[email].get("token") == token:
                skipped_dup.append({"line": line_no, "source": source, "email": email, "reason": "Already exists with same token"})
                continue
            existing[email]["token"] = token
            overwritten.append({"line": line_no, "source": source, "email": email})
            added += 1
        else:
            existing[email] = {"token": token, "user": None}
            added += 1

    cfg["accounts"] = existing
    if not cfg.get("activeAccount") and existing:
        cfg["activeAccount"] = next(iter(existing))

    if added > 0:
        save_config(cfg)

    first_index = -1
    if added > 0 and existing:
        emails = list(existing.keys())
        changed = [o["email"] for o in overwritten]
        for source, line_no, email, token in raw_items:
            if email in changed or (email in existing and email not in [s["email"] for s in skipped_dup]):
                first_index = emails.index(email)
                break

    return jsonify({
        "success": added > 0 or (not errors and (skipped_dup or overwritten)),
        "added": added,
        "overwritten": overwritten,
        "skipped": skipped_dup,
        "errors": errors,
        "total": len(existing),
        "activeAccount": cfg.get("activeAccount"),
        "firstIndex": first_index,
    })


@app.route("/api/chat", methods=["POST"])
def api_chat():
    message = request.json.get("message", "")
    model = request.json.get("model", "deepseek-v4-flash")
    if "/" not in model:
        model = "deepseek/" + model
    stream = request.json.get("stream", True)

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": message}],
        "stream": stream,
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}

    proxy_url = f"http://localhost:{proxy_state['port']}/v1/chat/completions"

    try:
        r = requests.post(proxy_url, json=payload,
            headers={"Content-Type": "application/json"},
            stream=stream, timeout=120)
    except Exception as e:
        return jsonify({"error": f"Proxy unavailable: {e}"}), 502

    if stream:
        def generate():
            for chunk in r.iter_content(chunk_size=4096):
                if chunk:
                    yield chunk
        return Response(generate(), content_type=r.headers.get("content-type", "text/event-stream"))

    try:
        return jsonify(r.json())
    except Exception:
        return jsonify({"error": r.text[:500]}), r.status_code


@app.route("/api/call-log")
def api_call_log():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 10, type=int)
    return jsonify(call_log.get_page(page, per_page))


def main():
    parser = argparse.ArgumentParser(description="stagewise WebUI")
    parser.add_argument("--port", "-p", type=int, default=8080, help="WebUI port (default: 8080)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()

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
        print("-> No accounts. Proxy not started. Add accounts at /accounts to get started.")

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
