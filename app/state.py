from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional

log = logging.getLogger(__name__)

_lock = threading.Lock()
_OVERRIDES_PATH = "data/state_overrides.json"

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


# ── Persistence ───────────────────────────────────────────────────────────────

def _save_overrides() -> None:
    """Write current overrides to disk. Must be called while holding _lock."""
    data = {
        "trade_mode": TRADE_MODE,
        "paper_mode": _PAPER_MODE,
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
        # SESSION_INVALID and _EMERGENCY_STOP intentionally omitted — always reset on restart
    }
    try:
        os.makedirs(os.path.dirname(_OVERRIDES_PATH) or ".", exist_ok=True)
        with open(_OVERRIDES_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception as exc:
        log.warning("state: failed to save overrides to %s: %s", _OVERRIDES_PATH, exc)


def load_overrides_from_disk() -> None:
    """Restore persisted overrides from disk. Call once at application startup."""
    global TRADE_MODE, _PAPER_MODE
    global _MAX_LOTS_OVERRIDE, _MAX_DAILY_LOSS_OVERRIDE, _SL_PCT_OVERRIDE
    global _RR_RATIO_OVERRIDE, _DAILY_PROFIT_TARGET_OVERRIDE, _SELL_OPTIONS_PROFIT_PCT_OVERRIDE
    global _ENTRY_WINDOW_START_OVERRIDE, _ENTRY_WINDOW_END_OVERRIDE, _NO_ENTRY_ON_EXPIRY_DAY_OVERRIDE
    global _TRAILING_SL_ENABLED, _MAX_TRADES_PER_DAY_OVERRIDE, _MAX_OPEN_POSITIONS_OVERRIDE
    global _CAPITAL_PER_TRADE_OVERRIDE, _CONSECUTIVE_LOSSES_LIMIT_OVERRIDE

    if not os.path.exists(_OVERRIDES_PATH):
        return
    try:
        with open(_OVERRIDES_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        log.warning("state: could not load overrides from %s: %s", _OVERRIDES_PATH, exc)
        return

    with _lock:
        if data.get("trade_mode"):
            TRADE_MODE = data["trade_mode"]
        if "paper_mode" in data:
            _PAPER_MODE = data["paper_mode"]
        if "max_lots" in data:
            _MAX_LOTS_OVERRIDE = data["max_lots"]
        if "max_daily_loss" in data:
            _MAX_DAILY_LOSS_OVERRIDE = data["max_daily_loss"]
        if "sl_pct" in data:
            _SL_PCT_OVERRIDE = data["sl_pct"]
        if "rr_ratio" in data:
            _RR_RATIO_OVERRIDE = data["rr_ratio"]
        if "daily_profit_target" in data:
            _DAILY_PROFIT_TARGET_OVERRIDE = data["daily_profit_target"]
        if "sell_options_profit_pct" in data:
            _SELL_OPTIONS_PROFIT_PCT_OVERRIDE = data["sell_options_profit_pct"]
        if "entry_window_start" in data:
            _ENTRY_WINDOW_START_OVERRIDE = data["entry_window_start"]
        if "entry_window_end" in data:
            _ENTRY_WINDOW_END_OVERRIDE = data["entry_window_end"]
        if "no_entry_on_expiry_day" in data:
            _NO_ENTRY_ON_EXPIRY_DAY_OVERRIDE = data["no_entry_on_expiry_day"]
        if "trailing_sl_enabled" in data:
            _TRAILING_SL_ENABLED = data["trailing_sl_enabled"]
        if "max_trades_per_day" in data:
            _MAX_TRADES_PER_DAY_OVERRIDE = data["max_trades_per_day"]
        if "max_open_positions" in data:
            _MAX_OPEN_POSITIONS_OVERRIDE = data["max_open_positions"]
        if "capital_per_trade" in data:
            _CAPITAL_PER_TRADE_OVERRIDE = data["capital_per_trade"]
        if "consecutive_losses_limit" in data:
            _CONSECUTIVE_LOSSES_LIMIT_OVERRIDE = data["consecutive_losses_limit"]

    log.info("state: restored overrides from %s", _OVERRIDES_PATH)


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
        _save_overrides()


def toggle_trade_mode() -> str:
    global TRADE_MODE
    with _lock:
        TRADE_MODE = "SELL_OPTIONS" if TRADE_MODE == "BUY_OPTIONS" else "BUY_OPTIONS"
        _save_overrides()
        return TRADE_MODE


# ── Paper / live mode (DRY_RUN override) ─────────────────────────────────────

def is_paper_mode(env_default: bool) -> bool:
    """Return effective dry-run flag; state override takes priority over .env."""
    with _lock:
        return _PAPER_MODE if _PAPER_MODE is not None else env_default


def set_paper_mode(paper: Optional[bool]) -> None:
    """Pass None to clear the override and fall back to the .env DRY_RUN value."""
    global _PAPER_MODE
    with _lock:
        _PAPER_MODE = paper
        _save_overrides()


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
        _save_overrides()


def get_max_daily_loss(env_default: float) -> float:
    with _lock:
        return _MAX_DAILY_LOSS_OVERRIDE if _MAX_DAILY_LOSS_OVERRIDE is not None else env_default


def set_max_daily_loss(value: Optional[float]) -> None:
    global _MAX_DAILY_LOSS_OVERRIDE
    with _lock:
        _MAX_DAILY_LOSS_OVERRIDE = value if (value is not None and value > 0) else None
        _save_overrides()


def get_sl_pct(env_default: float) -> float:
    with _lock:
        return _SL_PCT_OVERRIDE if _SL_PCT_OVERRIDE is not None else env_default


def set_sl_pct(value: Optional[float]) -> None:
    global _SL_PCT_OVERRIDE
    with _lock:
        _SL_PCT_OVERRIDE = value if (value is not None and value > 0) else None
        _save_overrides()


def get_rr_ratio(env_default: float) -> float:
    with _lock:
        return _RR_RATIO_OVERRIDE if _RR_RATIO_OVERRIDE is not None else env_default


def set_rr_ratio(value: Optional[float]) -> None:
    global _RR_RATIO_OVERRIDE
    with _lock:
        _RR_RATIO_OVERRIDE = value if (value is not None and value > 0) else None
        _save_overrides()


def get_daily_profit_target(env_default: float) -> float:
    with _lock:
        return _DAILY_PROFIT_TARGET_OVERRIDE if _DAILY_PROFIT_TARGET_OVERRIDE is not None else env_default


def set_daily_profit_target(value: Optional[float]) -> None:
    global _DAILY_PROFIT_TARGET_OVERRIDE
    with _lock:
        _DAILY_PROFIT_TARGET_OVERRIDE = value if (value is not None and value >= 0) else None
        _save_overrides()


def get_sell_options_profit_pct(env_default: float) -> float:
    with _lock:
        return _SELL_OPTIONS_PROFIT_PCT_OVERRIDE if _SELL_OPTIONS_PROFIT_PCT_OVERRIDE is not None else env_default


def set_sell_options_profit_pct(value: Optional[float]) -> None:
    global _SELL_OPTIONS_PROFIT_PCT_OVERRIDE
    with _lock:
        _SELL_OPTIONS_PROFIT_PCT_OVERRIDE = value if (value is not None and 0 < value <= 1) else None
        _save_overrides()


def get_entry_window_start(env_default: str) -> str:
    with _lock:
        return _ENTRY_WINDOW_START_OVERRIDE if _ENTRY_WINDOW_START_OVERRIDE is not None else env_default


def set_entry_window_start(value: Optional[str]) -> None:
    global _ENTRY_WINDOW_START_OVERRIDE
    with _lock:
        _ENTRY_WINDOW_START_OVERRIDE = value or None
        _save_overrides()


def get_entry_window_end(env_default: str) -> str:
    with _lock:
        return _ENTRY_WINDOW_END_OVERRIDE if _ENTRY_WINDOW_END_OVERRIDE is not None else env_default


def set_entry_window_end(value: Optional[str]) -> None:
    global _ENTRY_WINDOW_END_OVERRIDE
    with _lock:
        _ENTRY_WINDOW_END_OVERRIDE = value or None
        _save_overrides()


def get_no_entry_on_expiry_day(env_default: bool) -> bool:
    with _lock:
        return _NO_ENTRY_ON_EXPIRY_DAY_OVERRIDE if _NO_ENTRY_ON_EXPIRY_DAY_OVERRIDE is not None else env_default


def set_no_entry_on_expiry_day(value: Optional[bool]) -> None:
    global _NO_ENTRY_ON_EXPIRY_DAY_OVERRIDE
    with _lock:
        _NO_ENTRY_ON_EXPIRY_DAY_OVERRIDE = value
        _save_overrides()


# ── Trailing SL enabled ───────────────────────────────────────────────────────

def is_trailing_enabled() -> bool:
    with _lock:
        return _TRAILING_SL_ENABLED


def set_trailing_enabled(value: bool) -> None:
    global _TRAILING_SL_ENABLED
    with _lock:
        _TRAILING_SL_ENABLED = value
        _save_overrides()


def toggle_trailing_enabled() -> bool:
    global _TRAILING_SL_ENABLED
    with _lock:
        _TRAILING_SL_ENABLED = not _TRAILING_SL_ENABLED
        _save_overrides()
        return _TRAILING_SL_ENABLED


# ── Additional risk overrides ─────────────────────────────────────────────────

def get_max_trades_per_day(env_default: int) -> int:
    with _lock:
        return _MAX_TRADES_PER_DAY_OVERRIDE if _MAX_TRADES_PER_DAY_OVERRIDE is not None else env_default


def set_max_trades_per_day(value: Optional[int]) -> None:
    global _MAX_TRADES_PER_DAY_OVERRIDE
    with _lock:
        _MAX_TRADES_PER_DAY_OVERRIDE = value if (value is not None and value > 0) else None
        _save_overrides()


def get_max_open_positions(env_default: int) -> int:
    with _lock:
        return _MAX_OPEN_POSITIONS_OVERRIDE if _MAX_OPEN_POSITIONS_OVERRIDE is not None else env_default


def set_max_open_positions(value: Optional[int]) -> None:
    global _MAX_OPEN_POSITIONS_OVERRIDE
    with _lock:
        _MAX_OPEN_POSITIONS_OVERRIDE = value if (value is not None and value > 0) else None
        _save_overrides()


def get_capital_per_trade(env_default: float) -> float:
    with _lock:
        return _CAPITAL_PER_TRADE_OVERRIDE if _CAPITAL_PER_TRADE_OVERRIDE is not None else env_default


def set_capital_per_trade(value: Optional[float]) -> None:
    global _CAPITAL_PER_TRADE_OVERRIDE
    with _lock:
        _CAPITAL_PER_TRADE_OVERRIDE = value if (value is not None and value > 0) else None
        _save_overrides()


def get_consecutive_losses_limit(env_default: int) -> int:
    with _lock:
        return _CONSECUTIVE_LOSSES_LIMIT_OVERRIDE if _CONSECUTIVE_LOSSES_LIMIT_OVERRIDE is not None else env_default


def set_consecutive_losses_limit(value: Optional[int]) -> None:
    global _CONSECUTIVE_LOSSES_LIMIT_OVERRIDE
    with _lock:
        _CONSECUTIVE_LOSSES_LIMIT_OVERRIDE = value if (value is not None and value > 0) else None
        _save_overrides()


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
