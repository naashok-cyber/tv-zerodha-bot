"""Tests for app/config.py — all offline, no network, no .env file."""
from __future__ import annotations

import pytest
from zoneinfo import ZoneInfo

from app.config import (
    ExpiryRule,
    IST,
    UTC,
    ProductType,
    Settings,
    SizingMode,
)


def _s(**kwargs) -> Settings:
    """Construct Settings ignoring any .env file on disk."""
    return Settings(_env_file=None, **kwargs)


# ── Timezone constants ────────────────────────────────────────────────────────

def test_ist_constant():
    assert IST == ZoneInfo("Asia/Kolkata")


def test_utc_constant():
    assert UTC == ZoneInfo("UTC")


# ── Safe defaults ─────────────────────────────────────────────────────────────

def test_dry_run_default():
    assert _s().DRY_RUN is True


def test_trading_enabled_default():
    assert _s().TRADING_ENABLED is True


def test_pyotp_disabled_by_default():
    assert _s().PYOTP_AUTO_LOGIN is False


# ── Product & enums ───────────────────────────────────────────────────────────

def test_product_type_default():
    assert _s().PRODUCT_TYPE == ProductType.NRML


def test_product_type_string_coercion():
    assert _s(PRODUCT_TYPE="MIS").PRODUCT_TYPE == ProductType.MIS


def test_expiry_rule_default():
    assert _s().OPTION_EXPIRY_RULE == ExpiryRule.NEAREST_WEEKLY


def test_expiry_rule_string_coercion():
    s = _s(OPTION_EXPIRY_RULE="NEAREST_MONTHLY")
    assert s.OPTION_EXPIRY_RULE == ExpiryRule.NEAREST_MONTHLY


def test_sizing_mode_default():
    assert _s().SIZING_MODE == SizingMode.PREMIUM_BASED


def test_sizing_mode_string_coercion():
    s = _s(SIZING_MODE="UNDERLYING_RISK_BASED")
    assert s.SIZING_MODE == SizingMode.UNDERLYING_RISK_BASED


# ── Capital & risk ────────────────────────────────────────────────────────────

def test_capital_defaults():
    s = _s()
    assert s.CAPITAL_PER_TRADE == 10_000.0
    assert s.TOTAL_CAPITAL == 100_000.0
    assert s.RISK_PER_TRADE_PCT == 1.0


def test_max_daily_loss_defaults():
    s = _s()
    assert s.MAX_DAILY_LOSS_ABS == 2_000.0
    assert s.MAX_DAILY_LOSS_PCT == 2.0


def test_effective_max_daily_loss_both_equal():
    # 2% of ₹1L = ₹2000 == ABS ₹2000 → min = ₹2000
    assert _s().effective_max_daily_loss == 2_000.0


def test_effective_max_daily_loss_abs_lower():
    s = _s(MAX_DAILY_LOSS_ABS=1_000.0, TOTAL_CAPITAL=100_000.0, MAX_DAILY_LOSS_PCT=2.0)
    assert s.effective_max_daily_loss == 1_000.0


def test_effective_max_daily_loss_pct_lower():
    # 1% of ₹1L = ₹1000 < ABS ₹2000
    s = _s(MAX_DAILY_LOSS_ABS=2_000.0, TOTAL_CAPITAL=100_000.0, MAX_DAILY_LOSS_PCT=1.0)
    assert s.effective_max_daily_loss == 1_000.0


def test_effective_max_daily_loss_fractional():
    # 1.5% of ₹80000 = ₹1200 < ABS ₹1500
    s = _s(MAX_DAILY_LOSS_ABS=1_500.0, TOTAL_CAPITAL=80_000.0, MAX_DAILY_LOSS_PCT=1.5)
    assert s.effective_max_daily_loss == pytest.approx(1_200.0)


def test_consecutive_losses_default():
    assert _s().CONSECUTIVE_LOSSES_LIMIT == 3


def test_rr_ratio_default():
    assert _s().RR_RATIO == 2.0


def test_market_protection_default():
    assert _s().MARKET_PROTECTION_PCT == -1.0


# ── Options defaults ──────────────────────────────────────────────────────────

def test_options_delta_defaults():
    s = _s()
    assert s.TARGET_DELTA == 0.65
    assert s.DELTA_TOLERANCE == 0.05


def test_sl_premium_pct_default():
    assert _s().SL_PREMIUM_PCT == pytest.approx(0.30)


def test_delta_translated_sl_disabled_by_default():
    assert _s().USE_DELTA_TRANSLATED_SL is False


def test_risk_free_rate():
    assert _s().RISK_FREE_RATE == pytest.approx(0.065)


def test_min_option_premiums():
    s = _s()
    assert s.MIN_OPTION_PREMIUM_INDEX == 5.0
    assert s.MIN_OPTION_PREMIUM_STOCK == 2.0


def test_min_oi_defaults():
    s = _s()
    assert s.MIN_OI_INDEX == 1_000
    assert s.MIN_OI_STOCK == 100


def test_max_spread_pct():
    assert _s().MAX_SPREAD_PCT == pytest.approx(0.05)


# ── Breakeven & trail ─────────────────────────────────────────────────────────

def test_breakeven_trail_defaults():
    s = _s()
    assert s.BREAKEVEN_RR == 1.0
    assert s.TRAIL_RR == 1.5
    assert s.TRAIL_DISTANCE_RR == 0.5


def test_breakeven_example():
    # entry=100, risk=30 (30% SL) → breakeven at entry + 1.0×30 = 130
    s = _s()
    entry, risk = 100.0, 30.0
    assert entry + s.BREAKEVEN_RR * risk == pytest.approx(130.0)


def test_trail_example():
    # entry=100, risk=30 → trail activates at entry + 1.5×30 = 145
    # trailing SL = current_premium − 0.5×30 = current_premium − 15
    s = _s()
    entry, risk = 100.0, 30.0
    assert entry + s.TRAIL_RR * risk == pytest.approx(145.0)
    assert s.TRAIL_DISTANCE_RR * risk == pytest.approx(15.0)


# ── IP allowlist & Natural Gas names ─────────────────────────────────────────

def test_tv_ips_all_present():
    ips = _s().TV_ALLOWED_IPS
    for ip in ("52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7"):
        assert ip in ips


def test_tv_ips_count():
    assert len(_s().TV_ALLOWED_IPS) == 4


def test_natural_gas_names_both_present():
    names = _s().NATURAL_GAS_NAMES
    assert "NATURALGAS" in names
    assert "NATGASMINI" in names


# ── Constructor override ──────────────────────────────────────────────────────

def test_constructor_override():
    s = _s(DRY_RUN=False, CAPITAL_PER_TRADE=5_000.0, MAX_TRADES_PER_DAY=5)
    assert s.DRY_RUN is False
    assert s.CAPITAL_PER_TRADE == 5_000.0
    assert s.MAX_TRADES_PER_DAY == 5


# ── Rate-limiting defaults ────────────────────────────────────────────────────

def test_max_ops_default():
    assert _s().MAX_OPS == 10


def test_backoff_defaults():
    s = _s()
    assert s.BACKOFF_MAX_TRIES == 5
    assert s.BACKOFF_INITIAL_WAIT_SECS == 1.0
