"""Tests for app/auth.py — all offline, no network."""
from __future__ import annotations

import time

import pytest

from app.auth import (
    AuthError,
    HMACError,
    IPBlockedError,
    IPRateLimiter,
    RateLimitError,
    check_ip,
    check_rate_limit,
    verify_hmac,
)

TV_IPS = ["52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7"]
NON_TV_IPS = [
    "1.2.3.4",
    "8.8.8.8",
    "0.0.0.0",
    "192.168.1.1",
    "10.0.0.1",
    "127.0.0.1",
    "::1",
    "2001:db8::1",
]


# ── IP allowlist ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ip", TV_IPS)
def test_tv_ip_is_allowed(ip: str) -> None:
    check_ip(ip, allowed_ips=TV_IPS)  # must not raise


@pytest.mark.parametrize("ip", NON_TV_IPS)
def test_non_tv_ip_is_blocked(ip: str) -> None:
    with pytest.raises(IPBlockedError):
        check_ip(ip, allowed_ips=TV_IPS)


def test_ip_blocked_error_is_subclass_of_auth_error() -> None:
    with pytest.raises(AuthError):
        check_ip("9.9.9.9", allowed_ips=TV_IPS)


def test_empty_allowlist_blocks_all_including_tv_ips() -> None:
    with pytest.raises(IPBlockedError):
        check_ip("52.89.214.238", allowed_ips=[])


def test_single_ip_allowlist() -> None:
    check_ip("1.2.3.4", allowed_ips=["1.2.3.4"])
    with pytest.raises(IPBlockedError):
        check_ip("1.2.3.5", allowed_ips=["1.2.3.4"])


# ── HMAC / webhook secret ─────────────────────────────────────────────────────

def test_hmac_correct_secret_does_not_raise() -> None:
    verify_hmac("my-secret", expected_secret="my-secret")


def test_hmac_wrong_secret_raises() -> None:
    with pytest.raises(HMACError):
        verify_hmac("wrong", expected_secret="correct")


def test_hmac_empty_payload_secret_raises() -> None:
    with pytest.raises(HMACError):
        verify_hmac("", expected_secret="correct")


def test_hmac_empty_configured_secret_raises() -> None:
    with pytest.raises(HMACError):
        verify_hmac("anything", expected_secret="")


def test_hmac_both_empty_raises() -> None:
    # Empty configured secret → not set; must not silently succeed.
    with pytest.raises(HMACError):
        verify_hmac("", expected_secret="")


def test_hmac_is_case_sensitive() -> None:
    with pytest.raises(HMACError):
        verify_hmac("Secret", expected_secret="secret")


def test_hmac_error_is_subclass_of_auth_error() -> None:
    with pytest.raises(AuthError):
        verify_hmac("bad", expected_secret="good")


def test_hmac_whitespace_matters() -> None:
    with pytest.raises(HMACError):
        verify_hmac("secret ", expected_secret="secret")


# ── IPRateLimiter — token bucket behaviour ────────────────────────────────────

def test_rate_limiter_allows_up_to_capacity() -> None:
    limiter = IPRateLimiter(capacity=5, refill_rate=0.001)
    for _ in range(5):
        assert limiter.is_allowed("1.1.1.1") is True


def test_rate_limiter_blocks_on_capacity_plus_one() -> None:
    limiter = IPRateLimiter(capacity=3, refill_rate=0.001)
    for _ in range(3):
        limiter.is_allowed("1.1.1.1")
    assert limiter.is_allowed("1.1.1.1") is False


def test_rate_limiter_zero_capacity_always_blocks() -> None:
    limiter = IPRateLimiter(capacity=0, refill_rate=0.0)
    assert limiter.is_allowed("1.1.1.1") is False


def test_rate_limiter_ips_are_independent() -> None:
    limiter = IPRateLimiter(capacity=2, refill_rate=0.001)
    for _ in range(2):
        limiter.is_allowed("ip-a")
    assert limiter.is_allowed("ip-a") is False
    # Exhausting ip-a must not affect ip-b
    assert limiter.is_allowed("ip-b") is True


def test_rate_limiter_refills_over_time() -> None:
    # Windows timer resolution is ~15 ms; sleep 50 ms to guarantee at least one
    # full tick passes.  At 1000 tokens/sec × ≥0.015 s = ≥15 tokens refilled.
    limiter = IPRateLimiter(capacity=1, refill_rate=1_000.0)
    limiter.is_allowed("1.1.1.1")   # consume the single token
    time.sleep(0.050)
    assert limiter.is_allowed("1.1.1.1") is True


def test_rate_limiter_does_not_exceed_capacity_on_refill() -> None:
    limiter = IPRateLimiter(capacity=2, refill_rate=1_000.0)
    time.sleep(0.010)   # 10 ms → would add 10 tokens without capping
    allowed = 0
    for _ in range(10):
        if limiter.is_allowed("1.1.1.1"):
            allowed += 1
    assert allowed == 2  # capped at capacity=2


# ── check_rate_limit wrapper ──────────────────────────────────────────────────

def test_check_rate_limit_passes_first_request() -> None:
    limiter = IPRateLimiter(capacity=5, refill_rate=0.001)
    check_rate_limit("1.1.1.1", limiter=limiter)  # must not raise


def test_check_rate_limit_raises_after_exhaustion() -> None:
    limiter = IPRateLimiter(capacity=1, refill_rate=0.001)
    check_rate_limit("1.1.1.1", limiter=limiter)  # OK
    with pytest.raises(RateLimitError):
        check_rate_limit("1.1.1.1", limiter=limiter)


def test_rate_limit_error_is_subclass_of_auth_error() -> None:
    limiter = IPRateLimiter(capacity=0, refill_rate=0.0)
    with pytest.raises(AuthError):
        check_rate_limit("1.1.1.1", limiter=limiter)
