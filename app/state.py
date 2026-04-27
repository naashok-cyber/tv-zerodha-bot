from __future__ import annotations

import threading

_lock = threading.Lock()
SESSION_INVALID: bool = False


def get_session_invalid() -> bool:
    with _lock:
        return SESSION_INVALID


def set_session_invalid(value: bool) -> None:
    global SESSION_INVALID
    with _lock:
        SESSION_INVALID = value
