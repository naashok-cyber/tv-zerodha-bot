import json
import math
import logging
import threading
import time
import re
import csv
import os
from datetime import datetime
from flask import Flask, request, jsonify
from kiteconnect import KiteConnect
from config import API_KEY, ACCESS_TOKEN

logging.basicConfig(
    filename='logs/webhook.log',
    level=logging.INFO,
    format='%(asctime)s - %(message)s'
)

app = Flask(__name__)

# ==================================================
# PAPER TRADING MODE
# Set to True  → no real orders, full simulation
# Set to False → live trading on Zerodha
# ==================================================
PAPER_TRADING = True

# ==================================================
# CORE SETTINGS
# ==================================================
MAX_ORDERS_PER_DAY = 10
ALLOWED_SYMBOLS    = []
order_count_today  = 0
last_reset_date    = datetime.now().date()

# OCO pairs — futures SL/Target cancel tracking
oco_pairs = {}

# Options positions monitor
options_positions = {}

# Market protection — 2% buffer
MARKET_PROTECTION_PCT = 0.02

# ==================================================
# R:R = 1:1
# SL = 10% loss of premium
# Target = 10% profit of premium  (was 20%, now 1:1)
# ==================================================
OPTIONS_SL_PCT     = 0.10   # Risk   — 10% of premium
OPTIONS_TARGET_PCT = 0.10   # Reward — 10% of premium (1:1 R:R)

# Signal deduplication — prevents TradingView double-fire
# NOTE: voice orders use a separate dedup in voice_handler.py
last_signal          = {}   # {symbol_key: (action, timestamp)}
DEDUP_WINDOW_SECONDS = 10

# ==================================================
# PAPER TRADING — P&L TRACKER
# All paper trades logged to paper_trades.csv
# Running session totals tracked in memory
# ==================================================
PAPER_TRADES_FILE   = 'logs/paper_trades.csv'
paper_trade_log     = []    # in-memory list of all paper trades this session
paper_pnl_total     = 0.0   # cumulative P&L this session
paper_order_counter = 0     # auto-increment fake order IDs


def init_paper_trades_file():
    """Create CSV with headers if it doesn't exist."""
    os.makedirs('logs', exist_ok=True)
    if not os.path.exists(PAPER_TRADES_FILE):
        with open(PAPER_TRADES_FILE, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'date', 'time', 'underlying', 'symbol', 'strike',
                'action', 'quantity', 'entry_price', 'exit_price',
                'sl_price', 'target_price', 'exit_reason',
                'pnl_per_unit', 'pnl_total', 'delta', 'exchange'
            ])
        print(f'Paper trades file created: {PAPER_TRADES_FILE}')


def log_paper_trade(underlying, symbol, strike, action, quantity,
                    entry_price, exit_price, sl_price, target_price,
                    exit_reason, delta, exchange):
    """Append a completed paper trade to CSV and update session P&L."""
    global paper_pnl_total

    pnl_per_unit = round(exit_price - entry_price, 2)
    pnl_total    = round(pnl_per_unit * quantity, 2)
    paper_pnl_total += pnl_total

    now = datetime.now()
    row = {
        'date'        : now.strftime('%Y-%m-%d'),
        'time'        : now.strftime('%H:%M:%S'),
        'underlying'  : underlying,
        'symbol'      : symbol,
        'strike'      : strike,
        'action'      : action,
        'quantity'    : quantity,
        'entry_price' : entry_price,
        'exit_price'  : exit_price,
        'sl_price'    : sl_price,
        'target_price': target_price,
        'exit_reason' : exit_reason,
        'pnl_per_unit': pnl_per_unit,
        'pnl_total'   : pnl_total,
        'delta'       : delta,
        'exchange'    : exchange,
    }
    paper_trade_log.append(row)

    with open(PAPER_TRADES_FILE, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        writer.writerow(row)

    result_icon = '✅' if pnl_total >= 0 else '❌'
    print(f'')
    print(f'====================================')
    print(f'PAPER TRADE CLOSED {result_icon}')
    print(f'====================================')
    print(f'Symbol      : {symbol}')
    print(f'Entry       : ₹{entry_price}')
    print(f'Exit        : ₹{exit_price}')
    print(f'Reason      : {exit_reason}')
    print(f'P&L/unit    : ₹{pnl_per_unit}')
    print(f'P&L total   : ₹{pnl_total}  ({quantity} qty)')
    print(f'Session P&L : ₹{round(paper_pnl_total, 2)}')
    print(f'====================================')
    logging.info(
        f'PAPER CLOSED: {symbol} entry:{entry_price} exit:{exit_price} '
        f'reason:{exit_reason} pnl:{pnl_total} session_pnl:{round(paper_pnl_total,2)}'
    )
    return pnl_total


def place_order_paper(kite, **kwargs):
    """
    Paper trading wrapper for kite.place_order().
    Logs all order params, returns a fake order ID.
    Real Zerodha API is never called for orders.
    """
    global paper_order_counter
    paper_order_counter += 1
    fake_id = f'PAPER-{int(time.time())}-{paper_order_counter:03d}'

    print(f'')
    print(f'[PAPER] Order skipped — would have placed:')
    for k, v in kwargs.items():
        if v is not None:
            print(f'[PAPER]   {k:<20}: {v}')
    print(f'[PAPER]   fake_order_id       : {fake_id}')
    logging.info(f'PAPER ORDER: {kwargs} → fake_id:{fake_id}')

    return fake_id


# ==================================================
# INSTRUMENT CONFIG
# ==================================================
INSTRUMENT_CONFIG = {
    "BANKNIFTY"  : {
        "exchange"    : "NFO",
        "lot_size"    : 30,
        "auto_options": True,
        "kite_symbol" : "NSE:NIFTY BANK",
        "mcx_futures" : False
    },
    "NIFTY"      : {
        "exchange"    : "NFO",
        "lot_size"    : 65,
        "auto_options": True,
        "kite_symbol" : "NSE:NIFTY 50",
        "mcx_futures" : False
    },
    "FINNIFTY"   : {
        "exchange"    : "NFO",
        "lot_size"    : 60,
        "auto_options": True,
        "kite_symbol" : "NSE:NIFTY FIN SERVICE",
        "mcx_futures" : False
    },
    "MIDCPNIFTY" : {
        "exchange"    : "NFO",
        "lot_size"    : 120,
        "auto_options": True,
        "kite_symbol" : "NSE:NIFTY MID SELECT",
        "mcx_futures" : False
    },
    "CRUDEOILM"  : {
        "exchange"    : "MCX",
        "lot_size"    : 10,
        "auto_options": True,
        "kite_symbol" : None,
        "mcx_futures" : True
    },
    "GOLDM"      : {
        "exchange"    : "MCX",
        "lot_size"    : 10,
        "auto_options": True,
        "kite_symbol" : None,
        "mcx_futures" : True
    },
    "SILVERM"    : {
        "exchange"    : "MCX",
        "lot_size"    : 5,
        "auto_options": True,
        "kite_symbol" : None,
        "mcx_futures" : True
    },
    "NATURALGAS" : {
        "exchange"    : "MCX",
        "lot_size"    : 1250,
        "auto_options": False,
        "kite_symbol" : None,
        "mcx_futures" : True
    },
}

# Cache for MCX nearest futures symbols
mcx_symbol_cache = {}


# ==================================================
# KITE CONNECTION
# ==================================================
def get_kite():
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(ACCESS_TOKEN)
    return kite


# ==================================================
# DAILY ORDER COUNTER RESET
# ==================================================
def reset_daily_counter():
    global order_count_today, last_reset_date
    today = datetime.now().date()
    if today != last_reset_date:
        order_count_today = 0
        last_reset_date   = today


# ==================================================
# SAFETY CHECK
# ==================================================
def safety_check(action, symbol, quantity):
    reset_daily_counter()
    if order_count_today >= MAX_ORDERS_PER_DAY:
        return False, f'Daily order limit of {MAX_ORDERS_PER_DAY} reached'
    if ALLOWED_SYMBOLS and symbol not in ALLOWED_SYMBOLS:
        return False, f'Symbol {symbol} not in allowed list'
    if action not in ['BUY', 'SELL']:
        return False, f'Invalid action: {action}'
    if quantity <= 0:
        return False, f'Invalid quantity: {quantity}'
    return True, 'OK'


# ==================================================
# APPLY MARKET PROTECTION
# ==================================================
def apply_market_protection(price, action):
    if action == "BUY":
        protected = round(price * (1 + MARKET_PROTECTION_PCT), 1)
    else:
        protected = round(price * (1 - MARKET_PROTECTION_PCT), 1)
    print(f'Market protection ({action}): {price} → {protected}')
    return protected


# ==================================================
# GET MCX NEAREST FUTURES SYMBOL
# ==================================================
def get_mcx_nearest_futures_symbol(underlying):
    global mcx_symbol_cache

    if underlying in mcx_symbol_cache:
        return mcx_symbol_cache[underlying]

    kite        = get_kite()
    instruments = kite.instruments("MCX")
    today       = datetime.now().date()

    futures = [
        i for i in instruments
        if i["name"] == underlying
        and i["instrument_type"] == "FUT"
        and i["expiry"] > today
    ]

    if not futures:
        futures = [
            i for i in instruments
            if i["name"] == underlying
            and i["instrument_type"] == "FUT"
        ]

    if not futures:
        raise Exception(f'No MCX futures found for {underlying}')

    nearest = sorted(futures, key=lambda x: x["expiry"])[0]
    symbol  = f'MCX:{nearest["tradingsymbol"]}'

    print(f'MCX nearest futures: {underlying} → {symbol} (expiry: {nearest["expiry"]})')
    mcx_symbol_cache[underlying] = symbol
    return symbol


# ==================================================
# GET REAL TIME UNDERLYING PRICE
# ==================================================
def get_underlying_price(underlying, tv_price=None):
    if tv_price and float(tv_price) > 0:
        print(f'Using TradingView price: {tv_price}')
        return float(tv_price)

    config = INSTRUMENT_CONFIG.get(underlying.upper())
    if not config:
        raise Exception(f'Unknown instrument: {underlying}')

    kite = get_kite()

    if config["exchange"] == "NFO":
        kite_symbol = config["kite_symbol"]
        ltp_data    = kite.ltp(kite_symbol)
        price       = list(ltp_data.values())[0]["last_price"]
        print(f'Kite LTP {underlying}: {price}')
        return float(price)
    else:
        kite_symbol = get_mcx_nearest_futures_symbol(underlying)
        ltp_data    = kite.ltp(kite_symbol)
        price       = list(ltp_data.values())[0]["last_price"]
        print(f'Kite LTP {underlying} ({kite_symbol}): {price}')
        return float(price)


# ==================================================
# GET OPTION LTP FROM KITE
# ==================================================
def get_option_ltp(tradingsymbol, exchange):
    try:
        kite        = get_kite()
        kite_symbol = f'{exchange}:{tradingsymbol}'
        ltp_data    = kite.ltp(kite_symbol)
        ltp         = list(ltp_data.values())[0]["last_price"]
        print(f'Option LTP {tradingsymbol}: {ltp}')
        return float(ltp)
    except Exception as e:
        print(f'Option LTP failed: {e}')
        return None


# ==================================================
# BLACK SCHOLES DELTA
# ==================================================
def black_scholes_delta(S, K, T, r, sigma, option_type="CE"):
    try:
        from scipy.stats import norm
        if T <= 0:
            return 1.0 if option_type == "CE" else -1.0
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        return norm.cdf(d1) if option_type == "CE" else norm.cdf(d1) - 1
    except Exception as e:
        print(f"Delta error: {e}")
        return 0.0


def get_expiry_days(expiry_date):
    today = datetime.now().date()
    days  = (expiry_date - today).days
    return max(days, 1)


# ==================================================
# FIND BEST DELTA STRIKE
# ==================================================
def find_best_strike_kite(underlying, action, target_delta=0.65, tv_price=None):
    kite        = get_kite()
    option_type = "CE" if action == "BUY" else "PE"
    config      = INSTRUMENT_CONFIG.get(underlying.upper())

    if not config:
        raise Exception(f"Unknown instrument: {underlying}")

    opt_exchange = config["exchange"]

    current_price = get_underlying_price(underlying, tv_price)
    print(f"Underlying price (Kite): {current_price}")

    print(f"Loading {opt_exchange} instruments...")
    instruments = kite.instruments(opt_exchange)
    today       = datetime.now().date()

    options = [
        i for i in instruments
        if i["name"] == underlying
        and i["instrument_type"] == option_type
        and i["expiry"] > today
    ]

    if not options:
        raise Exception(f"No {option_type} options found for {underlying}")

    nearest_expiry = sorted(set(i["expiry"] for i in options))[0]
    options        = [i for i in options if i["expiry"] == nearest_expiry]
    print(f"Found {len(options)} {option_type} options expiring {nearest_expiry}")

    lower_bound      = current_price * 0.90
    upper_bound      = current_price * 1.10
    options_filtered = [
        i for i in options
        if lower_bound <= i["strike"] <= upper_bound
    ]

    if not options_filtered:
        options_filtered = options[:50]

    print(f"Checking {len(options_filtered)} strikes...")

    symbols   = [f"{opt_exchange}:{i['tradingsymbol']}" for i in options_filtered]
    ltp_batch = kite.ltp(symbols)

    T     = get_expiry_days(nearest_expiry) / 365
    r     = 0.065
    sigma = 0.18 if opt_exchange == "NFO" else 0.25

    best_symbol     = None
    best_strike     = None
    best_delta      = None
    best_delta_diff = float("inf")
    best_ltp        = None

    for opt in options_filtered:
        symbol_key = f"{opt_exchange}:{opt['tradingsymbol']}"
        ltp_data   = ltp_batch.get(symbol_key, {})
        ltp        = ltp_data.get("last_price", 0)
        K          = opt["strike"]

        if not ltp or ltp <= 0:
            continue

        delta     = black_scholes_delta(
            S=current_price, K=K, T=T,
            r=r, sigma=sigma,
            option_type=option_type
        )
        abs_delta = abs(delta)
        diff      = abs(abs_delta - target_delta)

        if diff < best_delta_diff:
            best_delta_diff = diff
            best_strike     = K
            best_symbol     = opt["tradingsymbol"]
            best_delta      = delta
            best_ltp        = float(ltp)

    if not best_symbol:
        raise Exception(f"Could not find option with delta near {target_delta}")

    print(f"Best Strike : {best_strike}")
    print(f"Best Symbol : {best_symbol}")
    print(f"Delta (BS)  : {round(best_delta, 4)} ✅")
    print(f"Real LTP    : {best_ltp} ✅")

    return best_symbol, best_strike, best_delta, best_ltp, None, nearest_expiry, opt_exchange


# ==================================================
# ROBUST REQUEST PARSER
# ==================================================
def parse_request_data():
    raw_body = b''
    try:
        raw_body     = request.data
        content_type = request.content_type or ''

        print(f'Content-Type : {content_type}')
        print(f'Raw body     : {raw_body[:300]}')
        logging.info(f'Raw body: {raw_body[:300]}')

        try:
            data = request.get_json(silent=True, force=True)
            if data and isinstance(data, dict):
                print(f'Parsed via Method 1')
                return data
        except Exception:
            pass

        try:
            text = raw_body.decode('utf-8').strip()
            data = json.loads(text)
            if data and isinstance(data, dict):
                print(f'Parsed via Method 2')
                return data
        except Exception:
            pass

        try:
            text  = raw_body.decode('utf-8').strip()
            fixed = text.replace("'", '"')
            data  = json.loads(fixed)
            if data and isinstance(data, dict):
                print(f'Parsed via Method 3')
                return data
        except Exception:
            pass

        try:
            text  = raw_body.decode('utf-8').strip()
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                data = json.loads(match.group())
                if data and isinstance(data, dict):
                    print(f'Parsed via Method 4')
                    return data
        except Exception:
            pass

        try:
            if request.form:
                data = {
                    k: v[0] if isinstance(v, list) and len(v) == 1 else v
                    for k, v in dict(request.form).items()
                }
                if data:
                    print(f'Parsed via Method 5')
                    return data
        except Exception:
            pass

        print(f'All parsing methods failed')
        logging.error(f'Parse failed. Body:{raw_body[:200]}')
        return None

    except Exception as e:
        print(f'Parser error: {e}')
        return None


# ==================================================
# OCO MONITOR
# ==================================================
def monitor_oco_orders():
    print('OCO Monitor started ✅')
    logging.info('OCO Monitor started')

    while True:
        try:
            if oco_pairs and not PAPER_TRADING:
                kite         = get_kite()
                orders       = kite.orders()
                order_status = {
                    str(o["order_id"]): o["status"]
                    for o in orders
                }

                pairs_to_remove = []

                for sl_id, target_id in list(oco_pairs.items()):
                    sl_status     = order_status.get(str(sl_id),     "UNKNOWN")
                    target_status = order_status.get(str(target_id), "UNKNOWN")

                    if target_status == "COMPLETE":
                        try:
                            kite.cancel_order(
                                variety=kite.VARIETY_REGULAR,
                                order_id=str(sl_id)
                            )
                            print(f'✅ Target hit! SL {sl_id} cancelled')
                            logging.info(f'OCO: Target hit, SL {sl_id} cancelled')
                        except Exception as e:
                            print(f'SL cancel failed: {e}')
                        pairs_to_remove.append(sl_id)

                    elif sl_status == "COMPLETE":
                        try:
                            kite.cancel_order(
                                variety=kite.VARIETY_REGULAR,
                                order_id=str(target_id)
                            )
                            print(f'✅ SL hit! Target {target_id} cancelled')
                            logging.info(f'OCO: SL hit, Target {target_id} cancelled')
                        except Exception as e:
                            print(f'Target cancel failed: {e}')
                        pairs_to_remove.append(sl_id)

                    elif sl_status in ["CANCELLED", "REJECTED"] and \
                         target_status in ["CANCELLED", "REJECTED"]:
                        pairs_to_remove.append(sl_id)

                for sl_id in pairs_to_remove:
                    oco_pairs.pop(sl_id, None)

        except Exception as e:
            print(f'OCO Monitor error: {e}')

        time.sleep(5)


# ==================================================
# OPTIONS POSITION MONITOR
# ==================================================
def monitor_options_positions():
    print('Options Position Monitor started ✅')
    logging.info('Options Position Monitor started')

    while True:
        try:
            if options_positions:
                kite = None if PAPER_TRADING else get_kite()

                for opt_symbol, pos in list(options_positions.items()):
                    try:
                        underlying         = pos["underlying"]
                        sl_price           = float(pos["sl_price"])
                        target_price       = float(pos["target_price"])
                        quantity           = pos["quantity"]
                        opt_exchange       = pos["opt_exchange"]
                        entry_price        = float(pos["entry_price"])
                        strike             = pos.get("strike", "")
                        real_delta         = pos.get("real_delta", 0)
                        exchange_orders_ok = pos.get("exchange_orders_placed", False)

                        if not PAPER_TRADING and exchange_orders_ok:
                            continue

                        opt_ltp = get_option_ltp(opt_symbol, opt_exchange)

                        if not opt_ltp or opt_ltp <= 0:
                            print(f'Monitor: No LTP for {opt_symbol}, skipping cycle')
                            continue

                        sl_hit     = opt_ltp <= sl_price
                        target_hit = opt_ltp >= target_price

                        print(f'Monitor {opt_symbol}: LTP={opt_ltp} '
                              f'SL={sl_price} Target={target_price} '
                              f'sl_hit={sl_hit} target_hit={target_hit}'
                              f'{" [PAPER]" if PAPER_TRADING else ""}')

                        if sl_hit or target_hit:
                            exit_reason = "TARGET HIT" if target_hit else "SL HIT"
                            result_icon = "✅" if target_hit else "❌"

                            print(f'')
                            print(f'====================================')
                            print(f'{exit_reason} {result_icon}'
                                  f'{" [PAPER]" if PAPER_TRADING else ""}')
                            print(f'Symbol     : {opt_symbol}')
                            print(f'Option LTP : {opt_ltp}')
                            print(f'SL         : {sl_price}')
                            print(f'Target     : {target_price}')
                            print(f'====================================')
                            logging.info(
                                f'{exit_reason} {opt_symbol} ltp:{opt_ltp}'
                                f'{" [PAPER]" if PAPER_TRADING else ""}'
                            )

                            options_positions.pop(opt_symbol, None)
                            last_signal.pop(underlying, None)
                            print(f'Position removed before exit — re-entry safe ✅')

                            exit_ltp = get_option_ltp(opt_symbol, opt_exchange)

                            if exit_ltp and exit_ltp > 0:
                                exit_price = apply_market_protection(exit_ltp, "SELL")
                            else:
                                exit_price = target_price if target_hit else sl_price
                                print(f'LTP unavailable — using {"target" if target_hit else "SL"} as exit price')

                            if PAPER_TRADING:
                                print(f'[PAPER] Exit SELL skipped — would have placed:')
                                print(f'[PAPER]   tradingsymbol : {opt_symbol}')
                                print(f'[PAPER]   transaction   : SELL')
                                print(f'[PAPER]   quantity      : {quantity}')
                                print(f'[PAPER]   order_type    : LIMIT')
                                print(f'[PAPER]   price         : {exit_price}')

                                log_paper_trade(
                                    underlying   = underlying,
                                    symbol       = opt_symbol,
                                    strike       = strike,
                                    action       = "BUY",
                                    quantity     = int(quantity),
                                    entry_price  = entry_price,
                                    exit_price   = exit_price,
                                    sl_price     = sl_price,
                                    target_price = target_price,
                                    exit_reason  = exit_reason,
                                    delta        = real_delta,
                                    exchange     = opt_exchange,
                                )

                            else:
                                exchange_obj = (
                                    kite.EXCHANGE_NFO
                                    if opt_exchange == "NFO"
                                    else kite.EXCHANGE_MCX
                                )

                                if exit_ltp and exit_ltp > 0:
                                    exit_order = kite.place_order(
                                        variety          = kite.VARIETY_REGULAR,
                                        exchange         = exchange_obj,
                                        tradingsymbol    = opt_symbol,
                                        transaction_type = kite.TRANSACTION_TYPE_SELL,
                                        quantity         = int(quantity),
                                        product          = kite.PRODUCT_MIS,
                                        order_type       = kite.ORDER_TYPE_LIMIT,
                                        price            = float(exit_price)
                                    )
                                else:
                                    print(f'LTP failed — retrying once...')
                                    time.sleep(1)
                                    opt_ltp_retry = get_option_ltp(opt_symbol, opt_exchange)
                                    if opt_ltp_retry and opt_ltp_retry > 0:
                                        exit_price = apply_market_protection(opt_ltp_retry, "SELL")
                                        exit_order = kite.place_order(
                                            variety          = kite.VARIETY_REGULAR,
                                            exchange         = exchange_obj,
                                            tradingsymbol    = opt_symbol,
                                            transaction_type = kite.TRANSACTION_TYPE_SELL,
                                            quantity         = int(quantity),
                                            product          = kite.PRODUCT_MIS,
                                            order_type       = kite.ORDER_TYPE_LIMIT,
                                            price            = float(exit_price)
                                        )
                                    else:
                                        print(f'Using IOC market order as last resort')
                                        exit_order = kite.place_order(
                                            variety          = kite.VARIETY_REGULAR,
                                            exchange         = exchange_obj,
                                            tradingsymbol    = opt_symbol,
                                            transaction_type = kite.TRANSACTION_TYPE_SELL,
                                            quantity         = int(quantity),
                                            product          = kite.PRODUCT_MIS,
                                            order_type       = kite.ORDER_TYPE_MARKET,
                                            validity         = kite.VALIDITY_IOC
                                        )

                                print(f'Exit order placed! ID: {exit_order}')
                                logging.info(f'Exit: {exit_order} {opt_symbol} {exit_reason}')

                            print(f'Position cleared — ready for next signal ✅')

                    except Exception as e:
                        print(f'Position monitor error {opt_symbol}: {e}')
                        logging.error(f'Position monitor error {opt_symbol}: {e}')

        except Exception as e:
            print(f'Options Monitor error: {e}')
            logging.error(f'Options Monitor error: {e}')

        time.sleep(5)


# ==================================================
# RESOLVE UNDERLYING FROM SYMBOL
# ==================================================
def resolve_underlying(symbol):
    symbol = symbol.upper().strip()
    for suffix in ["1!", "2!", "3!"]:
        symbol = symbol.replace(suffix, "")
    for name in INSTRUMENT_CONFIG.keys():
        if symbol.startswith(name):
            return name
    return None


# ==================================================
# SHOULD USE OPTIONS?
# ==================================================
def should_use_options(underlying, explicit_options_mode=None):
    if explicit_options_mode is not None:
        return explicit_options_mode
    config = INSTRUMENT_CONFIG.get(underlying)
    if config:
        return config["auto_options"]
    return False


# ==================================================
# PLACE OPTIONS ORDER
# ==================================================
def place_options_order(action, underlying, quantity, target_delta=0.65,
                        tv_price=None, sl_price=None, target_price=None):
    global order_count_today, options_positions, oco_pairs
    kite     = get_kite()
    mode_tag = '[PAPER] ' if PAPER_TRADING else ''

    print(f'{mode_tag}Finding best {action} option delta {target_delta} for {underlying}...')

    best_symbol, best_strike, best_delta, best_ltp, best_iv, \
    best_expiry, opt_exchange = find_best_strike_kite(
        underlying   = underlying,
        action       = action,
        target_delta = target_delta,
        tv_price     = tv_price
    )

    if not best_ltp or best_ltp <= 0:
        raise Exception(f'Could not get real LTP for {best_symbol}')

    entry_price  = apply_market_protection(best_ltp, "BUY")
    exchange_obj = kite.EXCHANGE_NFO if opt_exchange == "NFO" else kite.EXCHANGE_MCX

    prem_sl     = round(entry_price * (1 - OPTIONS_SL_PCT),     1)
    prem_target = round(entry_price * (1 + OPTIONS_TARGET_PCT), 1)
    sl_trigger  = prem_sl
    sl_limit    = round(prem_sl * 0.995, 1)

    print(f'Real LTP     : {best_ltp}')
    print(f'Entry price  : {entry_price}')
    print(f'SL price     : {prem_sl}  ({OPTIONS_SL_PCT*100:.0f}% loss)  [R:R 1:1]')
    print(f'Target price : {prem_target} ({OPTIONS_TARGET_PCT*100:.0f}% profit) [R:R 1:1]')

    print(f'{mode_tag}Placing entry LIMIT BUY...')
    if PAPER_TRADING:
        order_id = place_order_paper(
            kite,
            variety          = 'REGULAR',
            exchange         = opt_exchange,
            tradingsymbol    = best_symbol,
            transaction_type = 'BUY',
            quantity         = int(quantity),
            product          = 'MIS',
            order_type       = 'LIMIT',
            price            = float(entry_price)
        )
    else:
        order_id = kite.place_order(
            variety          = kite.VARIETY_REGULAR,
            exchange         = exchange_obj,
            tradingsymbol    = best_symbol,
            transaction_type = kite.TRANSACTION_TYPE_BUY,
            quantity         = int(quantity),
            product          = kite.PRODUCT_MIS,
            order_type       = kite.ORDER_TYPE_LIMIT,
            price            = float(entry_price)
        )

    order_count_today += 1
    print(f'Entry order: {order_id}')
    logging.info(
        f'Options entry{" [PAPER]" if PAPER_TRADING else ""}: '
        f'{order_id} {best_symbol} @ {entry_price}'
    )

    sl_order_id = None
    try:
        print(f'{mode_tag}Placing SL trigger:{sl_trigger} limit:{sl_limit}...')
        if PAPER_TRADING:
            sl_order_id = place_order_paper(
                kite,
                variety          = 'REGULAR',
                exchange         = opt_exchange,
                tradingsymbol    = best_symbol,
                transaction_type = 'SELL',
                quantity         = int(quantity),
                product          = 'MIS',
                order_type       = 'SL',
                trigger_price    = float(sl_trigger),
                price            = float(sl_limit)
            )
        else:
            sl_order_id = kite.place_order(
                variety          = kite.VARIETY_REGULAR,
                exchange         = exchange_obj,
                tradingsymbol    = best_symbol,
                transaction_type = kite.TRANSACTION_TYPE_SELL,
                quantity         = int(quantity),
                product          = kite.PRODUCT_MIS,
                order_type       = kite.ORDER_TYPE_SL,
                trigger_price    = float(sl_trigger),
                price            = float(sl_limit)
            )
        print(f'SL order: {sl_order_id} trigger:₹{sl_trigger}')
        logging.info(
            f'Options SL{" [PAPER]" if PAPER_TRADING else ""}: '
            f'{sl_order_id} trigger:{sl_trigger}'
        )
    except Exception as e:
        print(f'SL order failed: {e}')
        logging.error(f'Options SL failed: {e}')

    target_order_id = None
    try:
        print(f'{mode_tag}Placing Target ₹{prem_target}...')
        if PAPER_TRADING:
            target_order_id = place_order_paper(
                kite,
                variety          = 'REGULAR',
                exchange         = opt_exchange,
                tradingsymbol    = best_symbol,
                transaction_type = 'SELL',
                quantity         = int(quantity),
                product          = 'MIS',
                order_type       = 'LIMIT',
                price            = float(prem_target)
            )
        else:
            target_order_id = kite.place_order(
                variety          = kite.VARIETY_REGULAR,
                exchange         = exchange_obj,
                tradingsymbol    = best_symbol,
                transaction_type = kite.TRANSACTION_TYPE_SELL,
                quantity         = int(quantity),
                product          = kite.PRODUCT_MIS,
                order_type       = kite.ORDER_TYPE_LIMIT,
                price            = float(prem_target)
            )
        print(f'Target order: {target_order_id} @ ₹{prem_target}')
        logging.info(
            f'Options target{" [PAPER]" if PAPER_TRADING else ""}: '
            f'{target_order_id} price:{prem_target}'
        )
    except Exception as e:
        print(f'Target order failed: {e}')
        logging.error(f'Options target failed: {e}')

    if sl_order_id and target_order_id:
        oco_pairs[str(sl_order_id)] = str(target_order_id)
        print(f'OCO registered: SL {sl_order_id} ↔ Target {target_order_id}')
        logging.info(f'OCO options: {sl_order_id} ↔ {target_order_id}')

    monitor_sl     = float(sl_price)     if sl_price     else prem_sl
    monitor_target = float(target_price) if target_price else prem_target

    exchange_orders_placed = (not PAPER_TRADING) and bool(sl_order_id and target_order_id)

    print(f'')
    print(f'====================================')
    print(f'Options Order Summary{" [PAPER]" if PAPER_TRADING else ""}')
    print(f'====================================')
    print(f'Symbol       : {best_symbol}')
    print(f'Exchange     : {opt_exchange}')
    print(f'Strike       : {best_strike}')
    print(f'Real Delta   : {round(best_delta, 4)}')
    print(f'Real LTP     : ₹{best_ltp}')
    print(f'Entry        : ₹{entry_price}')
    print(f'SL           : ₹{prem_sl}  ({OPTIONS_SL_PCT*100:.0f}% loss)  R:R 1:1')
    print(f'Target       : ₹{prem_target} ({OPTIONS_TARGET_PCT*100:.0f}% profit) R:R 1:1')
    print(f'Mode         : {"📄 PAPER TRADING" if PAPER_TRADING else "🔴 LIVE TRADING"}')
    print(f'====================================')

    logging.info(
        f'Options{" [PAPER]" if PAPER_TRADING else ""}: {order_id} {best_symbol} '
        f'delta:{round(best_delta,4)} entry:{entry_price} '
        f'sl:{prem_sl} target:{prem_target} rr:1:1'
    )

    options_positions[best_symbol] = {
        "underlying"            : underlying,
        "action"                : action,
        "quantity"              : quantity,
        "sl_price"              : monitor_sl,
        "target_price"          : monitor_target,
        "opt_exchange"          : opt_exchange,
        "entry_time"            : datetime.now().strftime("%H:%M:%S"),
        "entry_price"           : entry_price,
        "real_ltp"              : best_ltp,
        "real_delta"            : round(best_delta, 4),
        "real_iv"               : best_iv,
        "strike"                : best_strike,
        "exchange_orders_placed": exchange_orders_placed,
    }

    print(f'Position registered ✅')
    logging.info(
        f'Monitor: {best_symbol} SL:{monitor_sl} Target:{monitor_target} '
        f'exchange_orders:{exchange_orders_placed}'
    )

    return (order_id, best_symbol, best_strike, best_delta, entry_price,
            monitor_sl, monitor_target, best_ltp, best_iv)


# ==================================================
# PLACE FUTURES ORDER
# ==================================================
def place_order(action, symbol, quantity, stoploss=None, target=None,
                exchange="NFO", tv_price=None):
    global order_count_today, oco_pairs
    kite = get_kite()

    exchange_map = {
        "NFO": kite.EXCHANGE_NFO,
        "MCX": kite.EXCHANGE_MCX,
        "NSE": kite.EXCHANGE_NSE,
        "BSE": kite.EXCHANGE_BSE
    }
    kite_exchange       = exchange_map.get(exchange.upper(), kite.EXCHANGE_NFO)
    transaction_type    = kite.TRANSACTION_TYPE_BUY  if action.upper() == 'BUY'  else kite.TRANSACTION_TYPE_SELL
    sl_transaction_type = kite.TRANSACTION_TYPE_SELL if action.upper() == 'BUY'  else kite.TRANSACTION_TYPE_BUY

    underlying    = resolve_underlying(symbol)
    config        = INSTRUMENT_CONFIG.get(underlying, {}) if underlying else {}
    is_mcx        = config.get("mcx_futures", False)
    mode_tag      = '[PAPER] ' if PAPER_TRADING else ''

    current_price = get_underlying_price(underlying, tv_price)
    limit_price   = apply_market_protection(current_price, action)

    print(f'{mode_tag}{"MCX" if is_mcx else "NFO"} Futures LIMIT {action} at {limit_price}')

    if PAPER_TRADING:
        order_id = place_order_paper(
            kite,
            variety          = 'REGULAR',
            exchange         = exchange,
            tradingsymbol    = symbol.upper(),
            transaction_type = action,
            quantity         = int(quantity),
            product          = 'MIS',
            order_type       = 'LIMIT',
            price            = float(limit_price)
        )
    else:
        order_id = kite.place_order(
            variety          = kite.VARIETY_REGULAR,
            exchange         = kite_exchange,
            tradingsymbol    = symbol.upper(),
            transaction_type = transaction_type,
            quantity         = int(quantity),
            product          = kite.PRODUCT_MIS,
            order_type       = kite.ORDER_TYPE_LIMIT,
            price            = float(limit_price)
        )

    order_count_today += 1
    print(f'Main order: {order_id}')
    logging.info(
        f'Futures{" [PAPER]" if PAPER_TRADING else ""}: '
        f'{order_id} {action} {quantity} {symbol}'
    )

    sl_order_id = None
    if stoploss:
        try:
            sl_trigger = float(stoploss)
            sl_limit   = round(
                sl_trigger * (0.995 if action.upper() == 'BUY' else 1.005), 1
            )
            if PAPER_TRADING:
                sl_order_id = place_order_paper(
                    kite,
                    variety          = 'REGULAR',
                    exchange         = exchange,
                    tradingsymbol    = symbol.upper(),
                    transaction_type = 'SELL' if action.upper() == 'BUY' else 'BUY',
                    quantity         = int(quantity),
                    product          = 'MIS',
                    order_type       = 'SL',
                    trigger_price    = float(sl_trigger),
                    price            = float(sl_limit)
                )
            else:
                sl_order_id = kite.place_order(
                    variety          = kite.VARIETY_REGULAR,
                    exchange         = kite_exchange,
                    tradingsymbol    = symbol.upper(),
                    transaction_type = sl_transaction_type,
                    quantity         = int(quantity),
                    product          = kite.PRODUCT_MIS,
                    order_type       = kite.ORDER_TYPE_SL,
                    trigger_price    = float(sl_trigger),
                    price            = float(sl_limit)
                )
            print(f'SL order: {sl_order_id} trigger:{sl_trigger}')
            logging.info(f'SL{" [PAPER]" if PAPER_TRADING else ""}: {sl_order_id}')
        except Exception as e:
            print(f'SL failed: {e}')
            logging.error(f'SL failed: {e}')

    target_order_id = None
    if target:
        try:
            if PAPER_TRADING:
                target_order_id = place_order_paper(
                    kite,
                    variety          = 'REGULAR',
                    exchange         = exchange,
                    tradingsymbol    = symbol.upper(),
                    transaction_type = 'SELL' if action.upper() == 'BUY' else 'BUY',
                    quantity         = int(quantity),
                    product          = 'MIS',
                    order_type       = 'LIMIT',
                    price            = float(target)
                )
            else:
                target_order_id = kite.place_order(
                    variety          = kite.VARIETY_REGULAR,
                    exchange         = kite_exchange,
                    tradingsymbol    = symbol.upper(),
                    transaction_type = sl_transaction_type,
                    quantity         = int(quantity),
                    product          = kite.PRODUCT_MIS,
                    order_type       = kite.ORDER_TYPE_LIMIT,
                    price            = float(target)
                )
            print(f'Target order: {target_order_id} @ {target}')
            logging.info(f'Target{" [PAPER]" if PAPER_TRADING else ""}: {target_order_id}')
        except Exception as e:
            print(f'Target failed: {e}')
            logging.error(f'Target failed: {e}')

    if sl_order_id and target_order_id:
        oco_pairs[str(sl_order_id)] = str(target_order_id)
        print(f'OCO registered: {sl_order_id} ↔ {target_order_id}')

    return order_id, sl_order_id, target_order_id


# ==================================================
# EXECUTE ORDER — shared by /webhook and /voice/confirm
# Extracts + validates fields, runs safety checks,
# dispatches to place_options_order or place_order.
# Does NOT include the TradingView dedup — that stays
# in /webhook only. Voice dedup lives in voice_handler.
# ==================================================
def execute_order(data: dict, source: str = 'tradingview') -> tuple:
    action           = str(data.get('action',   '')).upper().strip()
    raw_symbol       = str(data.get('symbol',   '')).upper().strip()
    symbol           = raw_symbol.replace('1!', '').replace('2!', '').replace('3!', '')
    quantity         = int(float(data.get('quantity', 1)))
    stoploss         = data.get('stoploss', None)
    target           = data.get('target',   None)
    exchange         = str(data.get('exchange', 'NFO')).upper().strip()
    tv_price         = float(data.get('current_price', 0) or 0)
    options_mode_raw = data.get('options_mode', None)
    explicit_options = bool(options_mode_raw) if options_mode_raw is not None else None
    raw_underlying   = str(data.get('underlying', '')).upper().strip()
    underlying       = raw_underlying.replace('1!', '').replace('2!', '').replace('3!', '')
    target_delta     = float(data.get('target_delta', 0.65) or 0.65)

    if not underlying and symbol:
        underlying = resolve_underlying(symbol) or ''
    use_options = should_use_options(underlying, explicit_options)

    src_tag = f'[{source.upper()}] '

    if not action:
        return {'error': 'action is required'}, 400
    if not use_options and not symbol:
        return {'error': 'symbol required for futures mode'}, 400
    if use_options and not underlying:
        return {'error': 'underlying required for options mode'}, 400

    check_symbol = underlying if use_options else symbol
    safe, reason = safety_check(action, check_symbol, quantity)
    if not safe:
        print(f'{src_tag}Safety check failed: {reason}')
        logging.warning(f'{src_tag}Safety check failed: {reason}')
        return {'error': reason}, 400

    print(f'{src_tag}Safety checks passed!')

    if use_options and action == 'BUY':
        open_for_underlying = [
            sym for sym, pos in options_positions.items()
            if pos.get('underlying') == underlying
        ]
        if open_for_underlying:
            msg = (f'Entry blocked — position already open for '
                   f'{underlying}: {open_for_underlying}')
            print(f'⚠️  {msg}')
            logging.warning(f'{src_tag}{msg}')
            return {'status': 'ignored', 'reason': msg}, 200

    if use_options:
        print(f'{src_tag}→ OPTIONS mode: {underlying}')
        (order_id, best_symbol, best_strike, best_delta,
         entry_price, monitor_sl, monitor_target,
         real_ltp, real_iv) = place_options_order(
            action       = action,
            underlying   = underlying,
            quantity     = quantity,
            target_delta = target_delta,
            tv_price     = tv_price if tv_price > 0 else None,
            sl_price     = stoploss,
            target_price = target
        )
        print(f'{src_tag}Orders today: {order_count_today}/{MAX_ORDERS_PER_DAY}')
        return {
            'status'           : 'success',
            'source'           : source,
            'paper_trading'    : PAPER_TRADING,
            'mode'             : 'options',
            'order_id'         : str(order_id),
            'option_symbol'    : best_symbol,
            'strike'           : best_strike,
            'real_delta'       : round(best_delta, 4),
            'real_ltp'         : real_ltp,
            'real_iv'          : real_iv,
            'entry_price'      : entry_price,
            'sl_pct'           : f'{OPTIONS_SL_PCT*100:.0f}%',
            'target_pct'       : f'{OPTIONS_TARGET_PCT*100:.0f}%',
            'rr_ratio'         : '1:1',
            'monitoring_sl'    : monitor_sl,
            'monitoring_target': monitor_target,
            'action'           : action,
            'underlying'       : underlying,
            'quantity'         : quantity,
            'orders_today'     : order_count_today,
            'session_pnl'      : round(paper_pnl_total, 2),
            'paper_trades'     : len(paper_trade_log),
        }, 200
    else:
        print(f'{src_tag}→ FUTURES mode: {symbol}')
        order_id, sl_order_id, target_order_id = place_order(
            action, symbol, quantity, stoploss, target, exchange,
            tv_price=tv_price if tv_price > 0 else None
        )
        print(f'{src_tag}Orders today: {order_count_today}/{MAX_ORDERS_PER_DAY}')
        return {
            'status'          : 'success',
            'source'          : source,
            'paper_trading'   : PAPER_TRADING,
            'mode'            : 'futures',
            'order_id'        : str(order_id),
            'sl_order_id'     : str(sl_order_id)     if sl_order_id     else 'No SL',
            'target_order_id' : str(target_order_id) if target_order_id else 'No Target',
            'oco_registered'  : bool(sl_order_id and target_order_id),
            'action'          : action,
            'symbol'          : symbol,
            'exchange'        : exchange,
            'quantity'        : quantity,
            'stoploss'        : stoploss,
            'target'          : target,
            'orders_today'    : order_count_today,
            'session_pnl'     : round(paper_pnl_total, 2),
        }, 200


# ==================================================
# EXIT POSITION — voice EXIT_ALL / SQUARE_OFF
# Closes all open options positions for an underlying.
# Uses the same paper/live branching as the rest of
# the system. Does NOT go through execute_order.
# ==================================================
def exit_position(underlying: str, source: str = 'voice_manual') -> tuple:
    src_tag    = f'[{source.upper()}] '
    underlying = underlying.upper()

    open_pos = {
        sym: pos for sym, pos in options_positions.items()
        if pos.get('underlying') == underlying
    }
    if not open_pos:
        return {'error': f'No open positions for {underlying}'}, 404

    results  = []
    kite_obj = None if PAPER_TRADING else get_kite()

    for opt_symbol, pos in list(open_pos.items()):
        try:
            opt_exchange = pos['opt_exchange']
            quantity     = int(pos['quantity'])

            exit_ltp   = get_option_ltp(opt_symbol, opt_exchange)
            exit_price = apply_market_protection(exit_ltp, 'SELL') \
                         if (exit_ltp and exit_ltp > 0) else None

            # Remove from monitor BEFORE placing order
            options_positions.pop(opt_symbol, None)
            last_signal.pop(underlying, None)

            if PAPER_TRADING:
                kw = dict(
                    variety='REGULAR', exchange=opt_exchange,
                    tradingsymbol=opt_symbol, transaction_type='SELL',
                    quantity=quantity, product='MIS',
                )
                if exit_price:
                    kw.update(order_type='LIMIT', price=float(exit_price))
                else:
                    kw.update(order_type='MARKET')
                order_id  = place_order_paper(get_kite(), **kw)
                exit_used = exit_price or float(pos.get('entry_price', 0))
                log_paper_trade(
                    underlying   = underlying,
                    symbol       = opt_symbol,
                    strike       = pos.get('strike', ''),
                    action       = 'BUY',
                    quantity     = quantity,
                    entry_price  = float(pos.get('entry_price', 0)),
                    exit_price   = float(exit_used),
                    sl_price     = float(pos.get('sl_price', 0)),
                    target_price = float(pos.get('target_price', 0)),
                    exit_reason  = 'VOICE_MANUAL_EXIT',
                    delta        = pos.get('real_delta', 0),
                    exchange     = opt_exchange,
                )
            else:
                exch_obj = (kite_obj.EXCHANGE_NFO
                            if opt_exchange == 'NFO' else kite_obj.EXCHANGE_MCX)
                if exit_price:
                    order_id = kite_obj.place_order(
                        variety          = kite_obj.VARIETY_REGULAR,
                        exchange         = exch_obj,
                        tradingsymbol    = opt_symbol,
                        transaction_type = kite_obj.TRANSACTION_TYPE_SELL,
                        quantity         = quantity,
                        product          = kite_obj.PRODUCT_MIS,
                        order_type       = kite_obj.ORDER_TYPE_LIMIT,
                        price            = float(exit_price),
                    )
                else:
                    order_id = kite_obj.place_order(
                        variety          = kite_obj.VARIETY_REGULAR,
                        exchange         = exch_obj,
                        tradingsymbol    = opt_symbol,
                        transaction_type = kite_obj.TRANSACTION_TYPE_SELL,
                        quantity         = quantity,
                        product          = kite_obj.PRODUCT_MIS,
                        order_type       = kite_obj.ORDER_TYPE_MARKET,
                        validity         = kite_obj.VALIDITY_IOC,
                    )

            logging.info(f'{src_tag}Exit: {order_id} {opt_symbol} VOICE_MANUAL_EXIT')
            results.append({'symbol': opt_symbol, 'order_id': str(order_id), 'status': 'exited'})

        except Exception as exc:
            logging.error(f'{src_tag}Exit error {opt_symbol}: {exc}')
            results.append({'symbol': opt_symbol, 'error': str(exc), 'status': 'failed'})

    return {
        'status'       : 'success',
        'source'       : source,
        'underlying'   : underlying,
        'exits'        : results,
        'paper_trading': PAPER_TRADING,
    }, 200


# ==================================================
# WEBHOOK ROUTE  (unchanged behavior)
# TradingView dedup is NOT applied to voice orders.
# ==================================================
# SECURITY NOTE: This endpoint has no authentication.
# TradingView supports custom headers (Webhook → Headers).
# Add an X-TV-Secret header check here as a follow-up.
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = parse_request_data()

        if not data:
            print(f'ERROR: Could not parse request')
            logging.error(f'Parse failed. CT:{request.content_type}')
            return jsonify({'error': 'Could not parse request. Check JSON format.'}), 400

        action           = str(data.get('action',   '')).upper().strip()
        raw_symbol       = str(data.get('symbol',   '')).upper().strip()
        symbol           = raw_symbol.replace('1!', '').replace('2!', '').replace('3!', '')
        quantity         = int(float(data.get('quantity', 1)))
        stoploss         = data.get('stoploss', None)
        target           = data.get('target',   None)
        exchange         = str(data.get('exchange', 'NFO')).upper().strip()
        tv_price         = float(data.get('current_price', 0) or 0)
        options_mode_raw = data.get('options_mode', None)
        explicit_options = bool(options_mode_raw) if options_mode_raw is not None else None
        raw_underlying   = str(data.get('underlying', '')).upper().strip()
        underlying       = raw_underlying.replace('1!', '').replace('2!', '').replace('3!', '')
        target_delta     = float(data.get('target_delta', 0.65) or 0.65)

        if not underlying and symbol:
            underlying = resolve_underlying(symbol) or ''
        use_options = should_use_options(underlying, explicit_options)

        print(f'')
        print(f'====================================')
        print(f'{"[PAPER] " if PAPER_TRADING else ""}Alert: {action} {quantity} {symbol}')
        print(f'Exchange       : {exchange}')
        print(f'Underlying     : {underlying}')
        print(f'Stop Loss      : {stoploss}')
        print(f'Target         : {target}')
        print(f'TV Price       : {tv_price if tv_price > 0 else "Not provided"}')
        print(f'Options Mode   : {use_options}')
        print(f'Target Delta   : {target_delta}')
        print(f'Active OCO     : {len(oco_pairs)} pairs')
        print(f'Options Pos    : {len(options_positions)} positions')
        print(f'Paper Trades   : {len(paper_trade_log)} closed')
        print(f'Session P&L    : ₹{round(paper_pnl_total, 2)}')
        print(f'Time           : {datetime.now().strftime("%H:%M:%S")}')
        print(f'====================================')

        logging.info(
            f'Alert{" [PAPER]" if PAPER_TRADING else ""}: '
            f'{action} {quantity} {symbol} Options:{use_options}'
        )

        # TradingView deduplication — voice orders bypass this via /voice/confirm
        now     = time.time()
        sig_key = underlying if use_options else symbol
        last_action, last_time = last_signal.get(sig_key, (None, 0))

        if last_action == action and (now - last_time) < DEDUP_WINDOW_SECONDS:
            msg = (f'Duplicate signal ignored: {action} {sig_key} '
                   f'({round(now - last_time, 1)}s — window {DEDUP_WINDOW_SECONDS}s)')
            print(f'⚠️  {msg}')
            logging.warning(msg)
            return jsonify({'status': 'ignored', 'reason': msg}), 200

        last_signal[sig_key] = (action, now)

        result, status_code = execute_order(data, source='tradingview')
        return jsonify(result), status_code

    except Exception as e:
        print(f'Error: {e}')
        logging.error(f'Error: {e}')
        return jsonify({'error': str(e)}), 500


# ==================================================
# TOKEN UPDATE ENDPOINT
# ==================================================
TOKEN_SECRET_KEY = 'TRADING_BOT_SECRET_2026'

@app.route('/update_token', methods=['POST'])
def update_token():
    try:
        data = parse_request_data()
        if not data:
            return jsonify({'error': 'No data received'}), 400

        access_token = data.get('access_token', '')
        secret_key   = data.get('secret_key',   '')

        if secret_key != TOKEN_SECRET_KEY:
            print(f'⚠️ Unauthorized token update attempt')
            logging.warning('Unauthorized token update attempt')
            return jsonify({'error': 'Unauthorized'}), 401

        if not access_token:
            return jsonify({'error': 'access_token required'}), 400

        import re as re_module
        config_path = 'config.py'
        with open(config_path, 'r') as f:
            content = f.read()

        new_content = re_module.sub(
            r'ACCESS_TOKEN\s*=\s*\".*?\"',
            f'ACCESS_TOKEN = "{access_token}"',
            content
        )

        with open(config_path, 'w') as f:
            f.write(new_content)

        print(f'✅ Access token updated at {datetime.now().strftime("%H:%M:%S")}')
        logging.info(f'Token updated at {datetime.now().strftime("%H:%M:%S")}')

        return jsonify({
            'status' : 'success',
            'message': 'Token updated successfully',
            'time'   : datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })

    except Exception as e:
        print(f'Token update error: {e}')
        logging.error(f'Token update error: {e}')
        return jsonify({'error': str(e)}), 500


# ==================================================
# PAPER SUMMARY ENDPOINT
# ==================================================
@app.route('/paper_summary', methods=['GET'])
def paper_summary():
    winners = [t for t in paper_trade_log if t['pnl_total'] >= 0]
    losers  = [t for t in paper_trade_log if t['pnl_total'] <  0]
    win_pct = round(len(winners) / len(paper_trade_log) * 100, 1) if paper_trade_log else 0

    return jsonify({
        'paper_trading'  : PAPER_TRADING,
        'session_pnl'    : round(paper_pnl_total, 2),
        'total_trades'   : len(paper_trade_log),
        'winners'        : len(winners),
        'losers'         : len(losers),
        'win_rate_pct'   : win_pct,
        'rr_ratio'       : '1:1',
        'sl_pct'         : f'{int(OPTIONS_SL_PCT*100)}%',
        'target_pct'     : f'{int(OPTIONS_TARGET_PCT*100)}%',
        'trades_file'    : PAPER_TRADES_FILE,
        'trades'         : paper_trade_log,
        'time'           : datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })


# ==================================================
# STATUS ROUTE
# ==================================================
@app.route('/status', methods=['GET'])
def status():
    winners = [t for t in paper_trade_log if t['pnl_total'] >= 0]
    win_pct = round(len(winners) / len(paper_trade_log) * 100, 1) if paper_trade_log else 0

    return jsonify({
        'status'            : 'running',
        'paper_trading'     : PAPER_TRADING,
        'rr_ratio'          : '1:1',
        'orders_today'      : order_count_today,
        'max_orders_per_day': MAX_ORDERS_PER_DAY,
        'active_oco_pairs'  : len(oco_pairs),
        'options_positions' : len(options_positions),
        'market_protection' : f'{int(MARKET_PROTECTION_PCT*100)}%',
        'sl_pct'            : f'{int(OPTIONS_SL_PCT*100)}%',
        'target_pct'        : f'{int(OPTIONS_TARGET_PCT*100)}%',
        'dedup_window_sec'  : DEDUP_WINDOW_SECONDS,
        'session_pnl'       : round(paper_pnl_total, 2),
        'paper_trades'      : len(paper_trade_log),
        'paper_win_rate'    : f'{win_pct}%',
        'positions_detail'  : {
            k: {
                'underlying'            : v['underlying'],
                'sl'                    : v['sl_price'],
                'target'                : v['target_price'],
                'entry_price'           : v['entry_price'],
                'entry_time'            : v['entry_time'],
                'real_delta'            : v.get('real_delta'),
                'exchange_orders_placed': v.get('exchange_orders_placed'),
            }
            for k, v in options_positions.items()
        },
        'instruments'       : {
            k: {
                'exchange' : v['exchange'],
                'lot_size' : v['lot_size'],
                'mode'     : 'OPTIONS' if v['auto_options'] else 'FUTURES'
            }
            for k, v in INSTRUMENT_CONFIG.items()
        },
        'time'              : datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })


# ==================================================
# HOME ROUTE
# ==================================================
@app.route('/', methods=['GET'])
def home():
    return jsonify({
        'message'      : 'Webhook server — Kite paid API active!',
        'paper_trading': PAPER_TRADING,
        'rr_ratio'     : '1:1',
    })


# ==================================================
# STARTUP TEST
# ==================================================
def test_on_startup():
    print('Testing Kite paid API...')
    kite = get_kite()

    print('\n1. Testing Kite LTP (real time prices):')
    for underlying, cfg in INSTRUMENT_CONFIG.items():
        try:
            if cfg["exchange"] == "NFO":
                ltp_data = kite.ltp(cfg["kite_symbol"])
                price    = list(ltp_data.values())[0]["last_price"]
            else:
                kite_sym = get_mcx_nearest_futures_symbol(underlying)
                ltp_data = kite.ltp(kite_sym)
                price    = list(ltp_data.values())[0]["last_price"]
            print(f'  {underlying:<12} : ₹{price} ✅')
        except Exception as e:
            print(f'  {underlying:<12} : ❌ {e}')

    print('\n2. Testing Delta calculation (Real Kite LTP + Black Scholes):')
    try:
        bn = find_best_strike_kite("BANKNIFTY", "BUY", target_delta=0.65)
        print(f'  BANKNIFTY : Strike={bn[1]} Delta={round(bn[2],4)} LTP=₹{bn[3]} ✅')
    except Exception as e:
        print(f'  BANKNIFTY : ❌ {e}')

    try:
        co = find_best_strike_kite("CRUDEOILM", "BUY", target_delta=0.65)
        print(f'  CRUDEOILM : Strike={co[1]} Delta={round(co[2],4)} LTP=₹{co[3]} ✅')
    except Exception as e:
        print(f'  CRUDEOILM : ❌ {e}')

    print(f'\n3. Config check:')
    print(f'  Mode   : {"📄 PAPER TRADING" if PAPER_TRADING else "🔴 LIVE TRADING"}')
    print(f'  R:R    : 1:1 ✅')
    print(f'  SL     : {int(OPTIONS_SL_PCT*100)}% of premium ✅')
    print(f'  Target : {int(OPTIONS_TARGET_PCT*100)}% of premium ✅')
    print(f'  Dedup  : {DEDUP_WINDOW_SECONDS}s window ✅')


# ==================================================
# MAIN
# ==================================================
if __name__ == '__main__':
    init_paper_trades_file()

    # Wire in voice channel (additive — does not touch /webhook)
    from voice_handler import create_voice_blueprint
    voice_bp = create_voice_blueprint(
        execute_order_fn  = execute_order,
        exit_position_fn  = exit_position,
        instrument_config = INSTRUMENT_CONFIG,
    )
    app.register_blueprint(voice_bp)
    print('Voice channel blueprint registered ✅')

    print('====================================')
    print('  TradingView Webhook Server')
    print('  Powered by Kite Paid API ✅')
    print(f'  {"📄 PAPER TRADING MODE" if PAPER_TRADING else "🔴 LIVE TRADING MODE"}')
    print('====================================')
    print('Webhook       : http://localhost:80/webhook')
    print('Status        : http://localhost:80/status')
    print('Paper Summary : http://localhost:80/paper_summary')
    print('Voice Channel : http://localhost:80/voice/transcribe')
    print('====================================')
    print(f'R:R Ratio  : 1:1')
    print(f'SL         : {int(OPTIONS_SL_PCT*100)}% of premium')
    print(f'Target     : {int(OPTIONS_TARGET_PCT*100)}% of premium')
    print('====================================')
    print(f'{"Instrument":<14} {"Exchange":<8} {"Lot":<6} {"Mode"}')
    print('--------------------------------------------------')
    for name, cfg in INSTRUMENT_CONFIG.items():
        mode = "OPTIONS" if cfg["auto_options"] else "FUTURES"
        print(f'  {name:<12} {cfg["exchange"]:<8} {cfg["lot_size"]:<6} {mode}')
    print('====================================')
    test_on_startup()
    print('====================================')

    oco_thread  = threading.Thread(target=monitor_oco_orders,       daemon=True)
    opts_thread = threading.Thread(target=monitor_options_positions, daemon=True)
    oco_thread.start()
    opts_thread.start()

    print('OCO Monitor          : Running ✅')
    print('Options Position Mon : Running ✅')
    print(f'Paper Trades File    : {PAPER_TRADES_FILE}')
    print('====================================')

    app.run(host='0.0.0.0', port=80, debug=False)
