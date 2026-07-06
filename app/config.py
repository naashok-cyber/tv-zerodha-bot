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
    # BUY_OPTIONS:  BUY signal → buy CE,  SELL signal → buy PE  (directional)
    # SELL_OPTIONS: BUY signal → sell PE, SELL signal → sell CE  (theta decay, opposite type)
    # RANGE_SELL:   BUY signal → sell CE, SELL signal → sell PE  (contrarian, same type; only when ADX < threshold)
    BUY_OPTIONS = "BUY_OPTIONS"
    SELL_OPTIONS = "SELL_OPTIONS"
    RANGE_SELL = "RANGE_SELL"


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
    KITE_USER_ID: str = ""          # required when PYOTP_AUTO_LOGIN=true
    KITE_PASSWORD: str = ""         # required when PYOTP_AUTO_LOGIN=true
    KITE_TOTP_SECRET: str = ""      # required when PYOTP_AUTO_LOGIN=true
    KITE_AUTO_LOGIN_TIME: str = "07:45"  # HH:MM IST; auto-login cron time

    # ── Telegram ──────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = ""  # notifier is a no-op when empty
    TELEGRAM_CHAT_ID: str = ""

    # ── Risk ──────────────────────────────────────────────────────────────────
    DAILY_PROFIT_TARGET: float = 0.0   # ₹ daily profit cap; 0 = disabled; new entries blocked once hit
    CAPITAL_PER_TRADE: float = 100_000.0      # ₹ premium budget per trade
    TOTAL_CAPITAL: float = 100_000.0          # ₹ 1 Lakh; base for % loss cap
    RISK_PER_TRADE_PCT: float = 1.0           # used in UNDERLYING_RISK_BASED mode
    MAX_DAILY_LOSS_ABS: float = 10_000.0      # ₹ absolute daily loss cap
    MAX_TRADES_PER_DAY: int = 15
    MAX_OPEN_POSITIONS: int = 10
    MAX_LOTS_PER_ORDER: int = 5               # exchange freeze-quantity guard for futures
    MAX_LOTS_PER_TRADE: int = 1               # hard cap: at most 1 lot per trade, all instruments
    CONSECUTIVE_LOSSES_LIMIT: int = 5         # circuit breaker; resets only on manual intervention
    RR_RATIO: float = 2.0                     # target_dist = RR_RATIO × sl_dist
    MARKET_PROTECTION_PCT: float = -1.0       # -1 = kiteconnect default; mandatory on MARKET/SL-M

    # ── Product ───────────────────────────────────────────────────────────────
    PRODUCT_TYPE: ProductType = ProductType.NRML

    # ── Options (v2) ──────────────────────────────────────────────────────────
    NO_ENTRY_ON_EXPIRY_DAY: bool = True      # block SELL_OPTIONS on weekly expiry day
    SELL_OPTIONS_PROFIT_PCT: float = 0.50    # fallback flat target (used only when /control override is set)
    # Formula-based profit target: target_pct = max(FLOOR, BASE − fill × SLOPE)
    # At ₹100 → ~52%, ₹300 → ~46%, ₹500 → ~40%, ₹700 → ~34%, ₹1000+ → 25% floor
    SELL_OPTIONS_PROFIT_BASE: float = 0.55   # starting % at zero premium
    SELL_OPTIONS_PROFIT_SLOPE: float = 0.0003 # % reduction per ₹1 of premium
    SELL_OPTIONS_PROFIT_FLOOR: float = 0.25  # minimum target % regardless of premium
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
    ADX_THRESHOLD: float = 20.0                     # RANGE_SELL mode: skip trade when ADX >= this value
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
    FUTURES_SL_PCT: float = 0.015   # SL distance as fraction of price for futures (NG, CRUDE, etc.)
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
    ENTRY_WINDOW_START: str = "09:30"         # HH:MM IST; block new entries before this
    ENTRY_WINDOW_END: str = "15:00"           # HH:MM IST; block new entries after this (MCX uses 23:00)
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

    # ── Straddle ──────────────────────────────────────────────────────────────
    STRADDLE_STRIKE_INTERVAL: float = 2.5       # fallback strike spacing when not in STRADDLE_STRIKE_INTERVALS
    STRADDLE_STRIKE_INTERVALS: dict[str, float] = {
        "NATURALGAS": 5.0, "NATGASMINI": 2.5,
        "CRUDEOIL": 50.0,
        "NIFTY": 50.0, "BANKNIFTY": 100.0, "FINNIFTY": 50.0,
        "MIDCPNIFTY": 25.0, "SENSEX": 100.0,
    }
    STRADDLE_MAX_SPREAD_PCT: float = 1.0        # max bid-ask spread % per leg (1% default)
    STRADDLE_SL_MULTIPLIER: float = 1.5         # combined SL = net_credit × this
    STRADDLE_PER_LEG_SL_MULTIPLIER: float = 1.5 # per-leg hard SL = entry_premium × this
    STRADDLE_FILL_TIMEOUT_SECS: int = 5         # seconds to wait for concurrent leg fills
    STRADDLE_DELTA_TOLERANCE: float = 0.15      # ATM sanity: expect |delta| in 0.35–0.65
    OCO_SLIPPAGE_BUFFER_PCT: float = 0.002      # GTT limit offset from trigger (0.2%) to fill on gap moves

    # ── Scheduled straddle ────────────────────────────────────────────────────
    SCHEDULED_STRADDLE_ENABLED: bool = False
    NG_STRADDLE_TIME: str = "22:05"
    NG_STRADDLE_QTY: int = 1
    NG_STRADDLE_ADX_THRESHOLD: float = 25.0   # skip if ADX >= this
    STRADDLE_SQUAREOFF_TIME: str = "23:20"    # HH:MM IST

    # ── ADX (for scheduled straddle gate) ─────────────────────────────────────
    ADX_PERIOD: int = 14
    ADX_CANDLE_INTERVAL: str = "10minute"     # Kite interval string for historical candles

    # ── NATURALGAS delta hedge (5-min cron) ───────────────────────────────────
    NG_DELTA_HEDGE_ENABLED: bool = False
    NG_DELTA_HEDGE_THRESHOLD: float = 400.0   # mmBtu; sell more lots when |net δ| exceeds this
    NG_DELTA_HEDGE_LIMIT_WAIT_SEC: int = 20   # cancel + MARKET escalation after this

    # ── NATURALGAS half-exit (profit-lock; piggybacks on delta-hedge cron) ────
    # Default True here (instead of in .env) so the running container picks it
    # up via a docker compose restart, without needing a recreate. Re-rebuilds
    # of the image will read .env normally.
    NG_HALF_EXIT_ENABLED: bool = True
    NG_HALF_EXIT_PNL_TRIGGER: float = 8000.0    # ₹ M2M-today threshold to fire
    NG_HALF_EXIT_FLAG_PATH: str = "data/ng_half_exit_done.flag"  # delete to re-arm

    # ── NG straddle-ladder (post half-exit trim) ──────────────────────────────
    # After the half-exit fires, this ladder trims 1 ATM straddle (1 short CE +
    # 1 short PE nearest F) each time today's m2m rises another
    # NG_STRADDLE_LADDER_STEP above the m2m at half-exit fire time.
    # State file records baseline and lots-closed count; delete to re-arm.
    NG_STRADDLE_LADDER_ENABLED: bool = True
    NG_STRADDLE_LADDER_STEP: float = 2000.0     # ₹ per ladder rung
    NG_STRADDLE_LADDER_STATE_PATH: str = "data/ng_ladder_state.json"

    # ── BANKNIFTY stop-loss / trailing (piggybacks on the same 5-min cron) ────
    # Fires when today's m2m on all BANKNIFTY legs <= trigger.
    # Trigger can be a loss limit (negative) OR a profit-lock floor (positive).
    # Closes ALL currently-open BANKNIFTY shorts at LIMIT, escalating to MARKET.
    BNF_STOP_LOSS_ENABLED: bool = True
    BNF_STOP_LOSS_TRIGGER: float = 3000.0       # ₹ M2M today; closes when m2m <= this
    BNF_STOP_LOSS_FLAG_PATH: str = "data/bnf_stop_loss_done.flag"  # delete to re-arm

    # ── Voice channel ──────────────────────────────────────────────────────────
    VOICE_AUTH_TOKEN: str = ""          # Required; 401 if empty or wrong
    ADMIN_AUTH_TOKEN: str = ""          # Required; 401 if empty or wrong
    ANTHROPIC_API_KEY: str = ""         # Required for NLU; 503 if empty
    OPENAI_API_KEY: str = ""            # Optional; needed only when TRANSCRIPTION_MODE=whisper
    VOICE_NLU_MODEL: str = "claude-sonnet-4-6"
    VOICE_ALLOWED_INSTRUMENTS: list[str] = [
        "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX",
        "CRUDEOIL", "GOLD", "SILVER",
        "NATURALGAS", "NATGASMINI",
    ]
    VOICE_NFO_INSTRUMENTS: list[str] = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]
    VOICE_MCX_INSTRUMENTS: list[str] = ["CRUDEOIL", "GOLD", "SILVER", "NATURALGAS", "NATGASMINI"]
    WEBHOOK_BLOCKED_UNDERLYINGS: list[str] = ["CRUDEOILM"]
    VOICE_MAX_LOTS: int = 5
    VOICE_CONFIRM_TTL_SECONDS: int = 60
    VOICE_DEDUP_SECONDS: int = 10
    VOICE_RATE_LIMIT: int = 30          # max requests per 60-second window per token
    VOICE_CONFIG_PATH: str = "data/voice_config.json"

    # ── Commodity debate agents (decision-support; no auto-execution) ─────────
    COMMODITY_AGENTS_ENABLED: bool = False
    COMMODITY_AGENTS_COMMODITIES: list[str] = [
        "NATURALGAS", "CRUDEOIL", "GOLD", "SILVER", "NIFTY", "BANKNIFTY",
    ]
    COMMODITY_AGENTS_INTERVAL_MIN: int = 30
    COMMODITY_AGENTS_LIVE: bool = False   # Phase-6 live-execution gate (not implemented)
    COMMODITY_AGENTS_WEB_SEARCH: bool = True
    COMMODITY_AGENT_MODEL_TREND: str = "claude-sonnet-5"
    COMMODITY_AGENT_MODEL_EVENT: str = "claude-sonnet-5"
    COMMODITY_AGENT_MODEL_VOL: str = "claude-sonnet-5"
    COMMODITY_AGENT_MODEL_JUDGE: str = "claude-opus-4-8"
    COMMODITY_BLACKOUT_PRE_HOURS: float = 3.0
    COMMODITY_BLACKOUT_POST_HOURS: float = 1.0
    COMMODITY_MAX_LOSS_PER_LOT: float = 15000.0   # ₹ worst-case per lot cap
    COMMODITY_MAX_CONCURRENT: int = 2
    COMMODITY_MAX_MARGIN_UTIL_PCT: float = 60.0
    COMMODITY_AGENT_MAX_LOTS: int = 5
    COMMODITY_NOTIFY_MIN_CONFIDENCE: float = 0.6   # Telegram push threshold
    COMMODITY_DELTA_ALERT_THRESHOLD: float = 0.20  # straddle net delta/lot drift alert


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
