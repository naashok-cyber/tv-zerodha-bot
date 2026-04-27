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
    CAPITAL_PER_TRADE: float = 10_000.0       # ₹ premium budget per trade
    TOTAL_CAPITAL: float = 100_000.0          # ₹ 1 Lakh; base for % loss cap
    RISK_PER_TRADE_PCT: float = 1.0           # used in UNDERLYING_RISK_BASED mode
    MAX_DAILY_LOSS_ABS: float = 2_000.0       # ₹ absolute; kill switch on whichever hits first
    MAX_DAILY_LOSS_PCT: float = 2.0           # % of TOTAL_CAPITAL
    MAX_TRADES_PER_DAY: int = 10
    MAX_OPEN_POSITIONS: int = 3
    CONSECUTIVE_LOSSES_LIMIT: int = 3         # circuit breaker; resets only on manual intervention
    RR_RATIO: float = 2.0                     # target_dist = RR_RATIO × sl_dist
    MARKET_PROTECTION_PCT: float = -1.0       # -1 = kiteconnect default; mandatory on MARKET/SL-M

    # ── Product ───────────────────────────────────────────────────────────────
    PRODUCT_TYPE: ProductType = ProductType.NRML

    # ── Options (v2) ──────────────────────────────────────────────────────────
    TARGET_DELTA: float = 0.65
    DELTA_TOLERANCE: float = 0.05
    OPTION_EXPIRY_RULE: ExpiryRule = ExpiryRule.NEAREST_WEEKLY
    MIN_DAYS_TO_EXPIRY_INDEX: int = 1
    MIN_DAYS_TO_EXPIRY_STOCK: int = 3
    WEEKLY_INDICES: list[str] = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]
    SKIP_EXPIRY_DAY_CUTOFF_HOUR: int = 14    # legacy; superseded by SKIP_EXPIRY_CUTOFF_NSE
    SKIP_EXPIRY_DAY_CUTOFF_MINUTE: int = 30
    SKIP_EXPIRY_CUTOFF_NSE: str = "14:30"    # HH:MM IST; roll NSE expiry after this time
    SKIP_EXPIRY_CUTOFF_MCX: str = "22:00"    # HH:MM IST; roll MCX expiry after this time
    SESSION_CLOSE_NSE: str = "15:30"         # HH:MM IST; used for time_to_expiry in greeks
    SESSION_CLOSE_MCX: str = "23:30"
    SL_PREMIUM_PCT: float = 0.30             # Option A: SL if premium drops 30%
    USE_DELTA_TRANSLATED_SL: bool = False    # Option B: translate underlying SL via delta
    MIN_OPTION_PREMIUM_INDEX: float = 5.0    # ₹; sub-₹5 index options are illiquid
    MIN_OPTION_PREMIUM_STOCK: float = 2.0
    MIN_OI_INDEX: int = 1_000
    MIN_OI_STOCK: int = 100
    MAX_SPREAD_PCT: float = 0.05             # 5% of LTP bid-ask limit
    SIZING_MODE: SizingMode = SizingMode.PREMIUM_BASED
    RISK_FREE_RATE: float = 0.065            # India 10-yr G-sec proxy for Black-Scholes / Black-76
    DIVIDEND_YIELD_DEFAULT: float = 0.0     # q for all underlyings not in OVERRIDES
    DIVIDEND_YIELD_OVERRIDES: dict[str, float] = {}  # per-symbol q; e.g. {"INFY": 0.025}
    NATURAL_GAS_NAMES: list[str] = ["NATURALGAS", "NATGASMINI"]  # route to future, not option
    FUTURES_SL_PCT: float = 0.005   # SL distance as fraction of price for NG near-month futures

    # ── Risk module (risk.py) — Decimal for monetary precision ───────────────
    RISK_PCT: Decimal = Decimal("0.01")        # 1% per-trade risk fraction (futures sizing)
    MAX_DAILY_LOSS: Decimal = Decimal("2000")  # absolute ₹ daily loss cap for risk.py
    SL_PERCENT: Decimal = Decimal("0.005")     # 0.5% futures SL distance fraction (risk.py default)

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
    LOGIN_REMINDER_TIME: str = "07:30"
    INSTRUMENTS_REFRESH_TIME: str = "08:30"
    NSE_SQUAREOFF_TIME: str = "15:15"   # kept for future MIS support; NRML has no auto-squareoff
    MCX_SQUAREOFF_TIME: str = "23:20"

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

    @property
    def effective_max_daily_loss(self) -> float:
        """Smaller of the absolute and percentage-based daily loss caps."""
        pct_limit = self.TOTAL_CAPITAL * self.MAX_DAILY_LOSS_PCT / 100.0
        return min(self.MAX_DAILY_LOSS_ABS, pct_limit)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
