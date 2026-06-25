import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
KEYS_PATH = BASE_DIR / "data" / "api_keys.json"
KEY_PREFIX = "sk-stagewise-"
DEFAULT_MONTHLY_TOKENS = 100_000_000
DEFAULT_MONTHLY_REQUESTS = 1_000_000


def _load():
    try:
        return json.loads(KEYS_PATH.read_text("utf-8"))
    except Exception:
        return {"keys": {}}


def _save(data):
    KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEYS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _month_key():
    return time.strftime("%Y-%m")


def create_key(name, monthly_tokens=None, monthly_requests=None):
    data = _load()
    raw = secrets.token_hex(24)
    key = f"{KEY_PREFIX}{raw}"
    key_id = secrets.token_hex(8)
    now = _now()
    data["keys"][key] = {
        "key_id": key_id,
        "name": name,
        "created_at": now,
        "enabled": True,
        "monthly_token_limit": monthly_tokens or DEFAULT_MONTHLY_TOKENS,
        "monthly_request_limit": monthly_requests or DEFAULT_MONTHLY_REQUESTS,
        "usage": {
            "total_tokens": 0,
            "total_requests": 0,
            "month": _month_key(),
            "month_tokens": 0,
            "month_requests": 0,
        },
    }
    _save(data)
    return key, key_id


def _resolve_key(data, key_or_id):
    """Resolve a full key or key_id to (full_key, info)."""
    # Try exact full key match first
    if key_or_id in data["keys"]:
        return key_or_id, data["keys"][key_or_id]
    # Try key_id match
    for full_key, info in data["keys"].items():
        if info.get("key_id") == key_or_id:
            return full_key, info
    return None, None


def delete_key(key_or_id):
    data = _load()
    full_key, _ = _resolve_key(data, key_or_id)
    if full_key is None:
        return False
    del data["keys"][full_key]
    _save(data)
    return True


def toggle_key(key_or_id, enabled=None):
    data = _load()
    full_key, info = _resolve_key(data, key_or_id)
    if info is None:
        return False
    if enabled is not None:
        info["enabled"] = enabled
    else:
        info["enabled"] = not info["enabled"]
    _save(data)
    return True


def list_keys():
    data = _load()
    now_month = _month_key()
    result = []
    for key, info in data["keys"].items():
        u = info["usage"]
        if u["month"] != now_month:
            u["month_tokens"] = 0
            u["month_requests"] = 0
            u["month"] = now_month
        token_pct = round(u["month_tokens"] / info["monthly_token_limit"] * 100, 2) if info["monthly_token_limit"] else 0
        req_pct = round(u["month_requests"] / info["monthly_request_limit"] * 100, 2) if info["monthly_request_limit"] else 0
        result.append({
            "key_id": info.get("key_id", ""),
            "keyPreview": key[:8] + "..." + key[-8:],
            "name": info["name"],
            "created_at": info["created_at"],
            "enabled": info["enabled"],
            "monthly_token_limit": info["monthly_token_limit"],
            "monthly_request_limit": info["monthly_request_limit"],
            "total_tokens": u["total_tokens"],
            "total_requests": u["total_requests"],
            "month_tokens": u["month_tokens"],
            "month_requests": u["month_requests"],
            "token_usage_pct": token_pct,
            "request_usage_pct": req_pct,
        })
    return result


def validate_key(key):
    data = _load()
    k = data["keys"].get(key)
    if k is None or not k["enabled"]:
        return None
    now_month = _month_key()
    u = k["usage"]
    if u["month"] != now_month:
        u["month_tokens"] = 0
        u["month_requests"] = 0
        u["month"] = now_month
    if u["month_tokens"] >= k["monthly_token_limit"]:
        return None
    if u["month_requests"] >= k["monthly_request_limit"]:
        return None
    return k


def record_usage(key, tokens, requests=1):
    data = _load()
    k = data["keys"].get(key)
    if k is None:
        return
    u = k["usage"]
    now_month = _month_key()
    if u["month"] != now_month:
        u["month_tokens"] = 0
        u["month_requests"] = 0
        u["month"] = now_month
    u["total_tokens"] += tokens
    u["total_requests"] += requests
    u["month_tokens"] += tokens
    u["month_requests"] += requests
    _save(data)


def extract_key_from_header(auth_header):
    if not auth_header:
        return None
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:].strip()
    if token.startswith(KEY_PREFIX):
        return token
    return None
