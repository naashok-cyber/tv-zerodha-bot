from __future__ import annotations

import hmac
import time
from threading import Lock

from app.config import get_settings


class AuthError(Exception):
    """Base class for all auth failures — catch this to handle any auth rejection."""


class IPBlockedError(AuthError):
    """Client IP is not in the TradingView allowlist."""


class HMACError(AuthError):
    """Webhook secret missing, unconfigured, or does not match."""


class RateLimitError(AuthError):
    """IP has exceeded the per-minute request budget."""


# Rate limiter: in-process token bucket — simpler than slowapi, no ASGI framework dep needed yet.
class IPRateLimiter:
    """Thread-safe per-IP token bucket.

    capacity  — maximum burst (tokens); also the per-minute sustained rate when
                refill_rate = capacity / 60.
    refill_rate — tokens added per second.
    """

    def __init__(self, capacity: int, refill_rate: float) -> None:
        self._capacity = float(capacity)
        self._refill_rate = refill_rate
        # ip → (current_tokens, last_refill_monotonic_time)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = Lock()

    def is_allowed(self, ip: str) -> bool:
        with self._lock:
            now = time.monotonic()
            tokens, last = self._buckets.get(ip, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - last) * self._refill_rate)
            if tokens >= 1.0:
                self._buckets[ip] = (tokens - 1.0, now)
                return True
            self._buckets[ip] = (tokens, now)
            return False


_default_limiter: IPRateLimiter | None = None
_default_limiter_lock = Lock()


def _get_default_limiter() -> IPRateLimiter:
    global _default_limiter
    if _default_limiter is None:
        with _default_limiter_lock:
            if _default_limiter is None:
                cap = get_settings().WEBHOOK_RATE_LIMIT_PER_MINUTE
                _default_limiter = IPRateLimiter(capacity=cap, refill_rate=cap / 60.0)
    return _default_limiter


def check_ip(
    client_ip: str,
    *,
    allowed_ips: list[str] | None = None,
) -> None:
    """Raise IPBlockedError if client_ip is not in the TradingView allowlist."""
    ips = allowed_ips if allowed_ips is not None else get_settings().TV_ALLOWED_IPS
    if client_ip not in ips:
        raise IPBlockedError(f"IP not in allowlist: {client_ip}")


def verify_hmac(payload_secret: str, *, expected_secret: str | None = None) -> None:
    """Constant-time comparison of webhook secret to prevent timing attacks.

    TradingView embeds the shared secret directly in the JSON payload's "secret"
    field. We compare it against WEBHOOK_SECRET using hmac.compare_digest so
    the comparison time is independent of how many leading bytes match.
    """
    secret = expected_secret if expected_secret is not None else get_settings().WEBHOOK_SECRET
    if not secret:
        raise HMACError("WEBHOOK_SECRET is not configured")
    if not hmac.compare_digest(
        payload_secret.encode("utf-8"),
        secret.encode("utf-8"),
    ):
        raise HMACError("Invalid webhook secret")


def check_rate_limit(
    client_ip: str,
    *,
    limiter: IPRateLimiter | None = None,
) -> None:
    """Raise RateLimitError if the per-IP token bucket is exhausted."""
    _lim = limiter if limiter is not None else _get_default_limiter()
    if not _lim.is_allowed(client_ip):
        raise RateLimitError(f"Rate limit exceeded for {client_ip}")
