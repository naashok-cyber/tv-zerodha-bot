from __future__ import annotations

from decimal import Decimal
from enum import Enum
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

# Module-level constants — import these directly rather than going through Settings.
IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


class ProductType(str, Enum):
    NRML = "NRML"
    MIS = "MIS"
    CNC = "CNC"


class ExpiryRule(str, Enum):
    NEAREST_WEEKLY = "NEAREST_WEEKLY"
    NEAREST_MONTHLY = "NEAREST_MONTHLY"


class SizingMode(str, Enum):
    PREMIUM_BASED = "PREMIUM_BASED"
    UNDERLYING_RISK_BASED = "UNDERLYING_RISK_BASED"


class TradeMode(str, Enum):
    # BUY_OPTIONS: BUY signal → buy CE, SELL signal → buy PE  (default)
    # SELL_OPTIONS: BUY signal → sell PE, SELL signal → sell CE  (write/short options)
    BUY_OPTIONS = "BUY_OPTIONS"
    SELL_OPTIONS = "SELL_OPTIONS"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── General ───────────────────────────────────────────────────────────────
    DRY_RUN: bool = True
    TRADING_ENABLED: bool = True  # kill switch; False blocks new entries, not exits
    LOG_LEVEL: str = "INFO"
    DATABASE_URL: str = "sqlite:///data/bot.db"

    # ── Security ──────────────────────────────────────────────────────────────
    # WEBHOOK_SECRET is empty by default; auth.py enforces non-empty at runtime
    # when DRY_RUN=False.
    WEBHOOK_SECRET: str = ""
    TV_ALLOWED_IPS: list[str] = [
        "52.89.214.238",
        "34.212.75.30",
        "54.218.53.128",
        "52.32.178.7",
    ]
    DASHBOARD_USERNAME: str = "admin"
    DASHBOARD_PASSWORD: str = ""

    # ── Kite Connect ──────────────────────────────────────────────────────────
    KITE_API_KEY: str = ""
    KITE_API_SECRET: str = ""
    KITE_REDIRECT_URL: str = ""
    KITE_ACCESS_TOKEN_FILE: str = "data/access_token.enc"
    KITE_MAX_TOKEN_AGE_HOURS: int = 20
    EXPECTED_EGRESS_IP: str = ""  # must match IP registered on developers.kite.trade
    PYOTP_AUTO_LOGIN: bool = False  # unofficial; keep False unless you accept Zerodha's stance

    # ── Telegram ──────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = ""  # notifier is a no-op when empty
    TELEGRAM_CHAT_ID: str = ""

    # ── Risk ──────────────────────────────────────────────────────────────────
    DAILY_PROFIT_TARGET: float = 0.0   # ₹ daily profit cap; 0 = disabled; new entries blocked once hit
    CAPITAL_PER_TRADE: float = 100_000.0      # ₹ premium budget per trade
    TOTAL_CAPITAL: float = 100_000.0          # ₹ 1 Lakh; base for % loss cap
    RISK_PER_TRADE_PCT: float = 1.0           # used in UNDERLYING_RISK_BASED mode
    MAX_DAILY_LOSS_ABS: float = 10_000.0      # ₹ absolute daily loss cap
    MAX_TRADES_PER_DAY: int = 3
    MAX_OPEN_POSITIONS: int = 3
    MAX_LOTS_PER_ORDER: int = 5               # exchange freeze-quantity guard for futures
    MAX_LOTS_PER_TRADE: int = 1               # hard cap: at most 1 lot per trade, all instruments
    CONSECUTIVE_LOSSES_LIMIT: int = 3         # circuit breaker; resets only on manual intervention
    RR_RATIO: float = 2.0                     # target_dist = RR_RATIO × sl_dist
    MARKET_PROTECTION_PCT: float = -1.0       # -1 = kiteconnect default; mandatory on MARKET/SL-M

    # ── Product ───────────────────────────────────────────────────────────────
    PRODUCT_TYPE: ProductType = ProductType.NRML

    # ── Options (v2) ──────────────────────────────────────────────────────────
    NO_ENTRY_ON_EXPIRY_DAY: bool = True      # block SELL_OPTIONS on weekly expiry day
    SELL_OPTIONS_PROFIT_PCT: float = 0.50    # exit short options when premium drops to this fraction of entry
    TARGET_DELTA: float = 0.65
    DELTA_FALLBACK_STEPS: list[float] = [0.50, 0.35, 0.25]  # tried in order when primary delta strike exceeds capital
    SELL_OPTIONS_TARGET_DELTA: float = 0.50          # ATM for writing options (SELL_OPTIONS mode)
    SELL_OPTIONS_DELTA_FALLBACK_STEPS: list[float] = [0.40, 0.30, 0.20]  # OTM fallback for writing
    SELL_OPTIONS_MAX_LOTS: int = 1                   # hard cap on lots per trade when writing options; margin-based, not premium-based
    DELTA_TOLERANCE: float = 0.05
    OPTION_EXPIRY_RULE: ExpiryRule = ExpiryRule.NEAREST_WEEKLY
    MIN_DAYS_TO_EXPIRY_INDEX: int = 1
    MIN_DAYS_TO_EXPIRY_STOCK: int = 2
    WEEKLY_INDICES: list[str] = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]
    SKIP_EXPIRY_DAY_CUTOFF_HOUR: int = 14    # legacy; superseded by SKIP_EXPIRY_CUTOFF_NSE
    SKIP_EXPIRY_DAY_CUTOFF_MINUTE: int = 30
    SKIP_EXPIRY_CUTOFF_NSE: str = "14:30"    # HH:MM IST; roll NSE expiry after this time
    SKIP_EXPIRY_CUTOFF_MCX: str = "22:00"    # HH:MM IST; roll MCX expiry after this time
    SESSION_CLOSE_NSE: str = "15:30"         # HH:MM IST; used for time_to_expiry in greeks
    SESSION_CLOSE_MCX: str = "23:30"
    SL_PREMIUM_PCT: float = 0.15             # Option A: SL if premium drops 15%; target = 2× SL dist (30%)
    USE_DELTA_TRANSLATED_SL: bool = False    # Option B: translate underlying SL via delta
    MIN_OPTION_PREMIUM_INDEX: float = 5.0    # ₹; sub-₹5 index options are illiquid
    MIN_OPTION_PREMIUM_STOCK: float = 2.0
    MIN_OI_INDEX: int = 1_000
    MIN_OI_STOCK: int = 100
    MAX_SPREAD_PCT: float = 0.05             # 5% of LTP bid-ask limit
    SIZING_MODE: SizingMode = SizingMode.PREMIUM_BASED
    TRADE_MODE: TradeMode = TradeMode.BUY_OPTIONS   # override in .env; can also be toggled live via /trade-mode/toggle
    RISK_FREE_RATE: float = 0.065            # India 10-yr G-sec proxy for Black-Scholes / Black-76
    DIVIDEND_YIELD_DEFAULT: float = 0.0     # q for all underlyings not in OVERRIDES
    DIVIDEND_YIELD_OVERRIDES: dict[str, float] = {}  # per-symbol q; e.g. {"INFY": 0.025}
    NATURAL_GAS_NAMES: list[str] = ["NATURALGAS", "NATGASMINI"]  # route to future, not option
    # MCX lot units: number of underlying units per lot (Kite quotes LTP per unit, orders in lots).
    # Kite instruments.csv stores lot_size=1 for MCX options; this map supplies the true contract size.
    MCX_LOT_UNITS: dict[str, int] = {
        "CRUDEOIL":   100,   # barrels/lot; LTP in INR/barrel
        "CRUDEOILM":  10,    # barrels/lot; LTP in INR/barrel
        "NATURALGAS": 1250,  # MMBtu/lot;   LTP in INR/MMBtu (routes to FUT but included for completeness)
        "NATGASMINI": 250,   # MMBtu/lot
        "GOLD":       100,   # × 10g units/lot (1 lot = 1 kg = 100 × 10 g); LTP in INR/10 g
        "GOLDM":      10,    # × 10g units/lot (1 lot = 100 g)
        "GOLDPETAL":  1,     # gram/lot; LTP in INR/gram
        "SILVER":     30,    # kg/lot (1 lot = 30 kg); LTP in INR/kg
        "SILVERM":    5,     # kg/lot (1 lot = 5 kg)
        "SILVERMIC":  1,     # kg/lot (1 lot = 1 kg)
        "COPPER":     2500,  # kg/lot (1 lot = 2.5 MT); LTP in INR/kg
        "ZINC":       5000,  # kg/lot (1 lot = 5 MT)
        "LEAD":       5000,  # kg/lot (1 lot = 5 MT)
        "ALUMINIUM":  5000,  # kg/lot (1 lot = 5 MT)
        "NICKEL":     1500,  # kg/lot (1 lot = 1500 kg)
    }
    FUTURES_SL_PCT: float = 0.008   # SL distance as fraction of price for NG near-month futures
    EQUITY_SL_PCT: float = 0.01     # SL = 1% of fill price for CNC equity trades; target = RR_RATIO × SL

    # ── Risk module (risk.py) — Decimal for monetary precision ───────────────
    RISK_PCT: Decimal = Decimal("0.05")        # 5% per-trade risk fraction → ₹5,000 on ₹1L capital (futures sizing)
    MAX_DAILY_LOSS: Decimal = Decimal("10000") # absolute ₹ daily loss cap for risk.py
    SL_PERCENT: Decimal = Decimal("0.008")     # 0.8% futures SL distance fraction (risk.py default)

    # ── Breakeven & Trail (on option premium; future price for NATURALGAS) ────
    # BUY CE at ₹100, SL_PREMIUM_PCT=30% → SL=₹70, risk=₹30:
    #   breakeven  : premium ≥ entry + BREAKEVEN_RR × risk  (₹130) → GTT SL → entry
    #   trail start: premium ≥ entry + TRAIL_RR × risk      (₹145) → trail activates
    #   trailing SL: current_premium − TRAIL_DISTANCE_RR × risk (₹15); never decreases
    # Trail fires only on TradingView "TRAIL" webhook (bar-close); never tick-level.
    BREAKEVEN_RR: float = 1.0
    TRAIL_RR: float = 1.5
    TRAIL_DISTANCE_RR: float = 0.5

    # ── Scheduler (IST, "HH:MM") ──────────────────────────────────────────────
    SCHEDULER_HOUR_IST: int = 8    # hour for daily_session_check cron job
    SCHEDULER_MINUTE_IST: int = 0  # minute for daily_session_check cron job
    LOGIN_REMINDER_TIME: str = "07:30"
    INSTRUMENTS_REFRESH_TIME: str = "08:30"
    NSE_SQUAREOFF_TIME: str = "15:25"   # daily EOD squareoff for open NFO positions
    MCX_SQUAREOFF_TIME: str = "23:25"
    ENTRY_WINDOW_START: str = "09:45"         # HH:MM IST; block new entries before this
    ENTRY_WINDOW_END: str = "14:30"           # HH:MM IST; block new entries after this
    EXPIRY_DAY_SQUAREOFF_TIME: str = "14:00"  # HH:MM IST; close expiry-day NFO positions early

    # ── Symbol / Instruments ──────────────────────────────────────────────────
    INSTRUMENTS_CSV_PATH: str = "data/instruments.csv"
    ROLLOVER_DAYS_BEFORE_EXPIRY: int = 1
    ATR_MULTIPLIER: float = 1.5
    NIFTY_FREEZE_QTY: int = 1800         # exchange single-order limit; slice via iceberg above this
    BANKNIFTY_FREEZE_QTY: int = 900

    # ── Encryption (token-at-rest) ────────────────────────────────────────────
    # Random 32+ char string; used to derive Fernet key via PBKDF2 for access_token encryption.
    SECRET_KEY: str = ""
    PBKDF2_ITERATIONS: int = 600_000   # OWASP 2023 minimum; tune so derivation takes ~100ms on your hardware

    # ── Rate limiting & retry ─────────────────────────────────────────────────
    MAX_OPS: int = 10                    # Kite API cap; above requires SEBI algo registration
    WEBHOOK_RATE_LIMIT_PER_MINUTE: int = 60  # per-IP token bucket capacity for inbound webhooks
    BACKOFF_MAX_TRIES: int = 5
    BACKOFF_INITIAL_WAIT_SECS: float = 1.0



@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
