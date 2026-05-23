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
_DAILY_PROFIT_TARGET_OVERRIDE: Optional[float] = None
_SELL_OPTIONS_PROFIT_PCT_OVERRIDE: Optional[float] = None
_ENTRY_WINDOW_START_OVERRIDE: Optional[str] = None  # "HH:MM"
_ENTRY_WINDOW_END_OVERRIDE: Optional[str] = None    # "HH:MM"
_NO_ENTRY_ON_EXPIRY_DAY_OVERRIDE: Optional[bool] = None
_TRAILING_SL_ENABLED: bool = True
_MAX_TRADES_PER_DAY_OVERRIDE: Optional[int] = None
_MAX_OPEN_POSITIONS_OVERRIDE: Optional[int] = None
_CAPITAL_PER_TRADE_OVERRIDE: Optional[float] = None
_CONSECUTIVE_LOSSES_LIMIT_OVERRIDE: Optional[int] = None


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


def get_daily_profit_target(env_default: float) -> float:
    with _lock:
        return _DAILY_PROFIT_TARGET_OVERRIDE if _DAILY_PROFIT_TARGET_OVERRIDE is not None else env_default


def set_daily_profit_target(value: Optional[float]) -> None:
    global _DAILY_PROFIT_TARGET_OVERRIDE
    with _lock:
        _DAILY_PROFIT_TARGET_OVERRIDE = value if (value is not None and value >= 0) else None


def get_sell_options_profit_pct(env_default: float) -> float:
    with _lock:
        return _SELL_OPTIONS_PROFIT_PCT_OVERRIDE if _SELL_OPTIONS_PROFIT_PCT_OVERRIDE is not None else env_default


def set_sell_options_profit_pct(value: Optional[float]) -> None:
    global _SELL_OPTIONS_PROFIT_PCT_OVERRIDE
    with _lock:
        _SELL_OPTIONS_PROFIT_PCT_OVERRIDE = value if (value is not None and 0 < value <= 1) else None


def get_entry_window_start(env_default: str) -> str:
    with _lock:
        return _ENTRY_WINDOW_START_OVERRIDE if _ENTRY_WINDOW_START_OVERRIDE is not None else env_default


def set_entry_window_start(value: Optional[str]) -> None:
    global _ENTRY_WINDOW_START_OVERRIDE
    with _lock:
        _ENTRY_WINDOW_START_OVERRIDE = value or None


def get_entry_window_end(env_default: str) -> str:
    with _lock:
        return _ENTRY_WINDOW_END_OVERRIDE if _ENTRY_WINDOW_END_OVERRIDE is not None else env_default


def set_entry_window_end(value: Optional[str]) -> None:
    global _ENTRY_WINDOW_END_OVERRIDE
    with _lock:
        _ENTRY_WINDOW_END_OVERRIDE = value or None


def get_no_entry_on_expiry_day(env_default: bool) -> bool:
    with _lock:
        return _NO_ENTRY_ON_EXPIRY_DAY_OVERRIDE if _NO_ENTRY_ON_EXPIRY_DAY_OVERRIDE is not None else env_default


def set_no_entry_on_expiry_day(value: Optional[bool]) -> None:
    global _NO_ENTRY_ON_EXPIRY_DAY_OVERRIDE
    with _lock:
        _NO_ENTRY_ON_EXPIRY_DAY_OVERRIDE = value


# ── Trailing SL enabled ───────────────────────────────────────────────────────

def is_trailing_enabled() -> bool:
    with _lock:
        return _TRAILING_SL_ENABLED


def set_trailing_enabled(value: bool) -> None:
    global _TRAILING_SL_ENABLED
    with _lock:
        _TRAILING_SL_ENABLED = value


def toggle_trailing_enabled() -> bool:
    global _TRAILING_SL_ENABLED
    with _lock:
        _TRAILING_SL_ENABLED = not _TRAILING_SL_ENABLED
        return _TRAILING_SL_ENABLED


# ── Additional risk overrides ─────────────────────────────────────────────────

def get_max_trades_per_day(env_default: int) -> int:
    with _lock:
        return _MAX_TRADES_PER_DAY_OVERRIDE if _MAX_TRADES_PER_DAY_OVERRIDE is not None else env_default


def set_max_trades_per_day(value: Optional[int]) -> None:
    global _MAX_TRADES_PER_DAY_OVERRIDE
    with _lock:
        _MAX_TRADES_PER_DAY_OVERRIDE = value if (value is not None and value > 0) else None


def get_max_open_positions(env_default: int) -> int:
    with _lock:
        return _MAX_OPEN_POSITIONS_OVERRIDE if _MAX_OPEN_POSITIONS_OVERRIDE is not None else env_default


def set_max_open_positions(value: Optional[int]) -> None:
    global _MAX_OPEN_POSITIONS_OVERRIDE
    with _lock:
        _MAX_OPEN_POSITIONS_OVERRIDE = value if (value is not None and value > 0) else None


def get_capital_per_trade(env_default: float) -> float:
    with _lock:
        return _CAPITAL_PER_TRADE_OVERRIDE if _CAPITAL_PER_TRADE_OVERRIDE is not None else env_default


def set_capital_per_trade(value: Optional[float]) -> None:
    global _CAPITAL_PER_TRADE_OVERRIDE
    with _lock:
        _CAPITAL_PER_TRADE_OVERRIDE = value if (value is not None and value > 0) else None


def get_consecutive_losses_limit(env_default: int) -> int:
    with _lock:
        return _CONSECUTIVE_LOSSES_LIMIT_OVERRIDE if _CONSECUTIVE_LOSSES_LIMIT_OVERRIDE is not None else env_default


def set_consecutive_losses_limit(value: Optional[int]) -> None:
    global _CONSECUTIVE_LOSSES_LIMIT_OVERRIDE
    with _lock:
        _CONSECUTIVE_LOSSES_LIMIT_OVERRIDE = value if (value is not None and value > 0) else None


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
            "daily_profit_target": _DAILY_PROFIT_TARGET_OVERRIDE,
            "sell_options_profit_pct": _SELL_OPTIONS_PROFIT_PCT_OVERRIDE,
            "entry_window_start": _ENTRY_WINDOW_START_OVERRIDE,
            "entry_window_end": _ENTRY_WINDOW_END_OVERRIDE,
            "no_entry_on_expiry_day": _NO_ENTRY_ON_EXPIRY_DAY_OVERRIDE,
            "trailing_sl_enabled": _TRAILING_SL_ENABLED,
            "max_trades_per_day": _MAX_TRADES_PER_DAY_OVERRIDE,
            "max_open_positions": _MAX_OPEN_POSITIONS_OVERRIDE,
            "capital_per_trade": _CAPITAL_PER_TRADE_OVERRIDE,
            "consecutive_losses_limit": _CONSECUTIVE_LOSSES_LIMIT_OVERRIDE,
        }
