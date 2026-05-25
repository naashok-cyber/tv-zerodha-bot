"""Thread-safe pending-order store with TTL expiry, dedup, rate limiting, and history."""
from __future__ import annotations

import time
from collections import deque
from threading import Lock
from typing import Optional


CONFIRM_TTL_SECONDS = 60
VOICE_DEDUP_SECONDS = 10


class PendingOrderStore:
    """All mutable state is protected by per-collection locks.

    Never mutate _orders, _dedup, _history, or _rate without holding the
    corresponding lock.  All public methods acquire exactly the locks they need.
    """

    def __init__(self) -> None:
        self._orders: dict[str, dict] = {}
        self._orders_lock = Lock()

        self._dedup: dict[str, float] = {}
        self._dedup_lock = Lock()

        self._history: deque[dict] = deque(maxlen=100)
        self._history_lock = Lock()

        self._rate: dict[str, deque] = {}
        self._rate_lock = Lock()

    # ── Pending orders ────────────────────────────────────────────────────────

    def store(self, token: str, entry: dict) -> None:
        with self._orders_lock:
            self._orders[token] = entry

    def get(self, token: str) -> Optional[dict]:
        with self._orders_lock:
            entry = self._orders.get(token)
        if entry is None:
            return None
        if time.monotonic() > entry["_expires"]:
            with self._orders_lock:
                self._orders.pop(token, None)
            return None
        return entry

    def pop(self, token: str) -> Optional[dict]:
        with self._orders_lock:
            return self._orders.pop(token, None)

    def advance_confirm_step(self, token: str) -> bool:
        with self._orders_lock:
            entry = self._orders.get(token)
            if entry is None:
                return False
            entry["_confirm_step"] = entry.get("_confirm_step", 1) + 1
            return True

    def expire_all(self) -> None:
        now = time.monotonic()
        with self._orders_lock:
            stale = [k for k, v in self._orders.items() if now > v["_expires"]]
            for k in stale:
                self._orders.pop(k, None)

    def list_active(self) -> list[dict]:
        now = time.monotonic()
        with self._orders_lock:
            items = [
                {
                    "token": v["_token"],
                    "action": v.get("action"),
                    "action_type": v.get("action_type"),
                    "underlying": v.get("underlying"),
                    "quantity": v.get("quantity"),
                    "confidence": v.get("_confidence"),
                    "low_confidence": v.get("_low_confidence"),
                    "is_exit": v.get("_is_exit"),
                    "confirm_step": v.get("_confirm_step"),
                    "transcript": v.get("_transcript"),
                    "expires_in_sec": round(v["_expires"] - now),
                    "source_ip": v.get("_source_ip"),
                }
                for v in self._orders.values()
                if now <= v["_expires"]
            ]
        return items

    def count(self) -> int:
        with self._orders_lock:
            return len(self._orders)

    # ── Voice dedup ────────────────────────────────────────────────────────────

    def is_duplicate(self, key: str) -> bool:
        now = time.monotonic()
        with self._dedup_lock:
            last = self._dedup.get(key, 0.0)
            if now - last < VOICE_DEDUP_SECONDS:
                return True
            self._dedup[key] = now
        return False

    # ── History ────────────────────────────────────────────────────────────────

    def add_history(self, entry: dict) -> None:
        with self._history_lock:
            self._history.appendleft(entry)

    def update_history(self, token: str, decision: str, result: Optional[dict]) -> None:
        with self._history_lock:
            for h in self._history:
                if h.get("_token") == token:
                    h["decision"] = decision
                    h["result"] = result
                    break

    def get_history(self, limit: int = 20) -> list[dict]:
        with self._history_lock:
            entries = list(self._history)[:limit]
        return [{k: v for k, v in h.items() if k != "_token"} for h in entries]

    def count_today(self, today_iso: str) -> int:
        with self._history_lock:
            return sum(
                1 for h in self._history
                if h.get("ts", "").startswith(today_iso)
                and h.get("decision") in ("approved_executed", "approved_failed")
            )

    # ── Rate limiting (per-token, 30 req / 60 s) ──────────────────────────────

    def check_rate(self, token: str, limit: int = 30, window_sec: int = 60) -> bool:
        now = time.monotonic()
        with self._rate_lock:
            q = self._rate.setdefault(token, deque())
            while q and now - q[0] > window_sec:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
        return True


# Module-level singleton — imported by route files.
_store = PendingOrderStore()


def get_store() -> PendingOrderStore:
    return _store
