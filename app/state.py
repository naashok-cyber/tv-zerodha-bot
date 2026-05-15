from __future__ import annotations

import threading

_lock = threading.Lock()
SESSION_INVALID: bool = False
TRADE_MODE: str = "BUY_OPTIONS"  # initialised from config in main.py lifespan


def get_session_invalid() -> bool:
    with _lock:
        return SESSION_INVALID


def set_session_invalid(value: bool) -> None:
    global SESSION_INVALID
    with _lock:
        SESSION_INVALID = value


def get_trade_mode() -> str:
    with _lock:
        return TRADE_MODE


def set_trade_mode(value: str) -> None:
    global TRADE_MODE
    with _lock:
        TRADE_MODE = value


def toggle_trade_mode() -> str:
    global TRADE_MODE
    with _lock:
        TRADE_MODE = "SELL_OPTIONS" if TRADE_MODE == "BUY_OPTIONS" else "BUY_OPTIONS"
        return TRADE_MODE
