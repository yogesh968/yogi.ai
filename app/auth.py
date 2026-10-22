"""Single-user dashboard login: signed, expiring session cookie + login rate limiting.

Only enforced when PUBLIC_BASE_URL is set (i.e. deployed). Locally, the server is loopback-only.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque

from fastapi import Request

from .config import get_settings

COOKIE = "assistant_session"
SESSION_DAYS = 30
_failures: dict[str, deque[float]] = defaultdict(deque)
MAX_FAILURES, WINDOW_S = 5, 600


def auth_required() -> bool:
    return bool(get_settings().public_base_url)


def _sign(msg: str) -> str:
    return hmac.new(get_settings().secret_key.encode(), msg.encode(), hashlib.sha256).hexdigest()


def make_session() -> str:
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    return f"{exp}.{_sign(exp)}"


def valid_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, _sign(exp))


def is_authenticated(request_cookies: dict[str, str]) -> bool:
    return not auth_required() or valid_session(request_cookies.get(COOKIE))


def client_ip(request: Request) -> str:
    # Behind Caddy on the same machine, trust X-Forwarded-For only from loopback.
    peer = request.client.host if request.client else ""
    if peer in ("127.0.0.1", "::1"):
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return peer


def rate_limited(ip: str) -> bool:
    q = _failures[ip]
    now = time.time()
    while q and now - q[0] > WINDOW_S:
        q.popleft()
    return len(q) >= MAX_FAILURES


def check_password(ip: str, password: str) -> bool:
    ok = hmac.compare_digest(password.encode(), get_settings().dashboard_password.encode())
    if not ok:
        _failures[ip].append(time.time())
    else:
        _failures.pop(ip, None)
    return ok


def check_webhook_secret(secret: str) -> bool:
    expected = get_settings().vobiz_webhook_secret
    return bool(expected) and hmac.compare_digest(secret.encode(), expected.encode())
