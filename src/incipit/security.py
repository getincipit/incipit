"""Transport-level web security policy shared by authentication responses."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import Response


class AttemptLimiter:
    """Small in-process sliding-window limiter for authentication attempts."""

    def __init__(self, limit: int = 10, window_seconds: int = 5 * 60):
        self.limit = limit
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allowed(self, identity: str) -> bool:
        now = time.monotonic()
        with self._lock:
            failures = self._failures[identity]
            while failures and failures[0] <= now - self.window_seconds:
                failures.popleft()
            return len(failures) < self.limit

    def failed(self, identity: str) -> None:
        with self._lock:
            self._failures[identity].append(time.monotonic())

    def consume(self, identity: str) -> bool:
        """Count every attempt and return whether it remains inside the window's allowance."""
        now = time.monotonic()
        with self._lock:
            attempts = self._failures[identity]
            while attempts and attempts[0] <= now - self.window_seconds:
                attempts.popleft()
            if len(attempts) >= self.limit:
                return False
            attempts.append(now)
            return True

    def succeeded(self, identity: str) -> None:
        with self._lock:
            self._failures.pop(identity, None)


def safe_local_redirect(target: str | None) -> str:
    """Return a same-site absolute path, never an external or header-injecting redirect."""
    target = (target or "").strip()
    if not target or any(ord(char) < 32 for char in target):
        return "/"
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return "/"
    return target


def add_browser_headers(response: Response) -> Response:
    """Apply browser hardening headers to every response that crosses the auth gate."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # A route that set a stricter policy of its own (an uploaded SVG's sandbox) keeps it.
    if "content-security-policy" not in response.headers:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; img-src 'self' data: blob:; "
            "connect-src 'self'; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
        )
    return response


def at_the_host(request: Request) -> bool:
    """Whether the request comes from a browser on the host computer itself, where the person installing
    the site sits: loopback, asked for by a local name, and not relayed by the tunnel (which also arrives
    from loopback, but under its public hostname and with Cloudflare's headers)."""
    client = request.client.host if request.client else ""
    hostname = request.headers.get("host", "").split(":")[0].lower()
    return (client in ("127.0.0.1", "::1") and hostname in ("localhost", "127.0.0.1", "[::1]")
            and "cf-connecting-ip" not in request.headers and "cf-ray" not in request.headers)
