from __future__ import annotations

import threading
from typing import Optional

_lock = threading.Lock()

# ── Session / mode state ──────────────────────────────────────────────────────
SESSION_INVALID: bool = False
TRADE_MODE: str = "BUY_OPTIONS"

# ── Live overrides — None means "use .env / config default" ──────────────────
_PAPER_MODE: Optional[bool] = None      # True=paper(dry-run), False=live; None=use .env
_EMERGENCY_STOP: bool = False           # blocks all new alert processing when True
_MAX_LOTS_OVERRIDE: Optional[int] = None
_MAX_DAILY_LOSS_OVERRIDE: Optional[float] = None
_SL_PCT_OVERRIDE: Optional[float] = None
_RR_RATIO_OVERRIDE: Optional[float] = None


# ── Session ───────────────────────────────────────────────────────────────────

def get_session_invalid() -> bool:
    with _lock:
        return SESSION_INVALID


def set_session_invalid(value: bool) -> None:
    global SESSION_INVALID
    with _lock:
        SESSION_INVALID = value


# ── Trade mode ────────────────────────────────────────────────────────────────

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


# ── Paper / live mode (DRY_RUN override) ─────────────────────────────────────

def is_paper_mode(env_default: bool) -> bool:
    """Return effective dry-run flag; state override takes priority over .env."""
    with _lock:
        return _PAPER_MODE if _PAPER_MODE is not None else env_default


def set_paper_mode(paper: bool) -> None:
    global _PAPER_MODE
    with _lock:
        _PAPER_MODE = paper


def get_paper_mode_override() -> Optional[bool]:
    with _lock:
        return _PAPER_MODE


# ── Emergency stop ────────────────────────────────────────────────────────────

def is_emergency_stop() -> bool:
    with _lock:
        return _EMERGENCY_STOP


def set_emergency_stop(stop: bool) -> None:
    global _EMERGENCY_STOP
    with _lock:
        _EMERGENCY_STOP = stop


# ── Risk parameter overrides ──────────────────────────────────────────────────

def get_max_lots(env_default: int) -> int:
    with _lock:
        return _MAX_LOTS_OVERRIDE if _MAX_LOTS_OVERRIDE is not None else env_default


def set_max_lots(value: Optional[int]) -> None:
    """Pass None or 0 to reset to .env default."""
    global _MAX_LOTS_OVERRIDE
    with _lock:
        _MAX_LOTS_OVERRIDE = value if (value is not None and value > 0) else None


def get_max_daily_loss(env_default: float) -> float:
    with _lock:
        return _MAX_DAILY_LOSS_OVERRIDE if _MAX_DAILY_LOSS_OVERRIDE is not None else env_default


def set_max_daily_loss(value: Optional[float]) -> None:
    global _MAX_DAILY_LOSS_OVERRIDE
    with _lock:
        _MAX_DAILY_LOSS_OVERRIDE = value if (value is not None and value > 0) else None


def get_sl_pct(env_default: float) -> float:
    with _lock:
        return _SL_PCT_OVERRIDE if _SL_PCT_OVERRIDE is not None else env_default


def set_sl_pct(value: Optional[float]) -> None:
    global _SL_PCT_OVERRIDE
    with _lock:
        _SL_PCT_OVERRIDE = value if (value is not None and value > 0) else None


def get_rr_ratio(env_default: float) -> float:
    with _lock:
        return _RR_RATIO_OVERRIDE if _RR_RATIO_OVERRIDE is not None else env_default


def set_rr_ratio(value: Optional[float]) -> None:
    global _RR_RATIO_OVERRIDE
    with _lock:
        _RR_RATIO_OVERRIDE = value if (value is not None and value > 0) else None


def get_all_overrides() -> dict:
    """Return current effective override state for display."""
    with _lock:
        return {
            "paper_mode": _PAPER_MODE,
            "emergency_stop": _EMERGENCY_STOP,
            "max_lots": _MAX_LOTS_OVERRIDE,
            "max_daily_loss": _MAX_DAILY_LOSS_OVERRIDE,
            "sl_pct": _SL_PCT_OVERRIDE,
            "rr_ratio": _RR_RATIO_OVERRIDE,
        }
