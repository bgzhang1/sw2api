import threading
import time

MAX_ENTRIES = 500
_log = []
_lock = threading.Lock()


def record(model, email, input_tokens, output_tokens, status="success"):
    entry = {
        "time": time.time(),
        "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": model or "unknown",
        "email": email or "unknown",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "status": status,
    }
    with _lock:
        _log.append(entry)
        if len(_log) > MAX_ENTRIES:
            del _log[:-MAX_ENTRIES]


def get_page(page=1, per_page=10):
    with _lock:
        total = len(_log)
        start = max(0, total - page * per_page)
        end = total - (page - 1) * per_page
        items = list(reversed(_log[start:end]))
        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        }
