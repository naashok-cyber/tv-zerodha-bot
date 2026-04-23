from flask import Flask, request, jsonify
from datetime import datetime
import time

app = Flask(__name__)

# ============================================
# CONFIG
# ============================================

MODE = "SIMULATION"
TRADING_ENABLED = True

MAX_OPEN_POSITIONS = 3
MAX_DAILY_LOSS = -2000
MAX_TRADES_PER_DAY = 10
MAX_CONSECUTIVE_LOSSES = 3

BREAKEVEN_RR = 1.0
TRAIL_RR = 1.5
TRAIL_STEP = 0.5

# ============================================
# STATE
# ============================================

trade_count = 0
daily_pnl = 0
open_positions = 0
consecutive_losses = 0

positions = []
closed_trades = []
signal_history = []
processed_signals = set()

last_signal_time = time.time()

# ============================================
# SYMBOL NORMALIZATION
# ============================================

def normalize_symbol(symbol):
    s = symbol.upper()

    if "BANKNIFTY" in s:
        return "BANKNIFTY"
    if "NIFTY" in s:
        return "NIFTY"
    if "CRUDE" in s:
        return "CRUDEOILM"
    if "NATURAL" in s:
        return "NATURALGAS"
    if "GOLD" in s:
        return "GOLDM"
    if "SILVER" in s:
        return "SILVERM"

    return s

# ============================================
# RISK ENGINE
# ============================================

def risk_check():

    if daily_pnl <= MAX_DAILY_LOSS:
        return False, "Daily loss limit hit"

    if trade_count >= MAX_TRADES_PER_DAY:
        return False, "Max trades reached"

    if open_positions >= MAX_OPEN_POSITIONS:
        return False, "Max positions reached"

    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        return False, "Too many consecutive losses"

    if not TRADING_ENABLED:
        return False, "Trading disabled"

    return True, "OK"

# ============================================
# TRADE ENGINE
# ============================================

def update_positions(current_price):

    global open_positions, daily_pnl, consecutive_losses

    to_close = []

    for p in positions:

        entry = p["entry"]
        sl = p["stoploss"]
        target = p["target"]
        action = p["action"]

        risk = abs(entry - sl)
        pnl = 0

        if action == "BUY":

            pnl = current_price - entry

            # Breakeven
            if current_price >= entry + risk * BREAKEVEN_RR:
                p["stoploss"] = max(p["stoploss"], entry)

            # Trailing
            if current_price >= entry + risk * TRAIL_RR:
                new_sl = current_price - risk * TRAIL_STEP
                p["stoploss"] = max(p["stoploss"], new_sl)

            if current_price <= p["stoploss"]:
                p["status"] = "SL HIT"
                to_close.append(p)

            elif current_price >= target:
                p["status"] = "TARGET HIT"
                to_close.append(p)

        elif action == "SELL":

            pnl = entry - current_price

            if current_price <= entry - risk * BREAKEVEN_RR:
                p["stoploss"] = min(p["stoploss"], entry)

            if current_price <= entry - risk * TRAIL_RR:
                new_sl = current_price + risk * TRAIL_STEP
                p["stoploss"] = min(p["stoploss"], new_sl)

            if current_price >= p["stoploss"]:
                p["status"] = "SL HIT"
                to_close.append(p)

            elif current_price <= target:
                p["status"] = "TARGET HIT"
                to_close.append(p)

        p["pnl"] = round(pnl, 2)

    # Close trades
    for p in to_close:

        positions.remove(p)
        open_positions -= 1
        daily_pnl += p["pnl"]

        p["exit_time"] = datetime.now().strftime("%H:%M:%S")
        closed_trades.append(p)

        if p["pnl"] < 0:
            consecutive_losses += 1
        else:
            consecutive_losses = 0

        signal_history.append({
            "time": p["exit_time"],
            "symbol": p["symbol"],
            "strategy": p["strategy"],
            "action": p["action"],
            "status": p["status"]
        })

# ============================================
# WEBHOOK
# ============================================

@app.route("/webhook", methods=["POST"])
def webhook():

    global trade_count, open_positions, last_signal_time

    data = request.get_json(force=True)

    if not data:
        return jsonify({"status": "invalid payload"}), 400

    signal_id = data.get("signal_id", str(time.time()))

    if signal_id in processed_signals:
        return jsonify({"status": "duplicate ignored"})

    processed_signals.add(signal_id)

    symbol = normalize_symbol(data.get("symbol", "UNKNOWN"))
    action = data.get("action", "NONE")
    strategy = data.get("strategy", "UNKNOWN")

    price = float(data.get("price", 0))
    stoploss = float(data.get("stoploss", 0))
    target = float(data.get("target", 0))

    last_signal_time = time.time()

    # Update existing trades first
    update_positions(price)

    allowed, reason = risk_check()

    if not allowed:
        return jsonify({"status": reason})

    # Create new trade
    trade_count += 1
    open_positions += 1

    position = {
        "symbol": symbol,
        "strategy": strategy,
        "action": action,
        "entry": price,
        "stoploss": stoploss,
        "target": target,
        "entry_time": datetime.now().strftime("%H:%M:%S"),
        "pnl": 0,
        "status": "OPEN"
    }

    positions.append(position)

    signal_history.append({
        "time": position["entry_time"],
        "symbol": symbol,
        "strategy": strategy,
        "action": action,
        "status": "TRADE EXECUTED"
    })

    if len(signal_history) > 10:
        signal_history.pop(0)

    return jsonify({"status": "trade executed"})

# ============================================
# DASHBOARD
# ============================================

@app.route("/dashboard")
def dashboard():

    rows = ""

    for s in reversed(signal_history):
        rows += f"""
        <tr>
        <td>{s['time']}</td>
        <td>{s['symbol']}</td>
        <td>{s['strategy']}</td>
        <td>{s['action']}</td>
        <td>{s['status']}</td>
        </tr>
        """

    return f"""
    <html>
    <body>

    <h2>Algo Trading Bot Dashboard</h2>

    <h3>Status</h3>
    Mode: {MODE}<br>
    Trading Enabled: {TRADING_ENABLED}<br>

    <h3>Trade Stats</h3>
    Trades Today: {trade_count}<br>
    Open Positions: {open_positions}<br>
    Daily PnL: ₹{round(daily_pnl,2)}<br>
    Consecutive Losses: {consecutive_losses}<br>

    <h3>Last Signals</h3>

    <table border="1">
    <tr>
    <th>Time</th>
    <th>Symbol</th>
    <th>Strategy</th>
    <th>Action</th>
    <th>Status</th>
    </tr>

    {rows}

    </table>

    <br>

    <a href="/positions">VIEW POSITIONS</a><br>
    <a href="/history">VIEW HISTORY</a><br><br>

    <a href="/start">START TRADING</a><br>
    <a href="/stop">STOP TRADING</a>

    </body>
    </html>
    """

# ============================================
# POSITIONS
# ============================================

@app.route("/positions")
def positions_page():

    if not positions:
        rows = "<tr><td>No open positions</td></tr>"
    else:
        rows = ""
        for p in positions:
            rows += f"""
            <tr>
            <td>{p['symbol']}</td>
            <td>{p['action']}</td>
            <td>{p['entry']}</td>
            <td>{p['stoploss']}</td>
            <td>{p['target']}</td>
            <td>{p['pnl']}</td>
            </tr>
            """

    return f"<h2>Open Positions</h2><table border=1>{rows}</table>"

# ============================================
# HISTORY
# ============================================

@app.route("/history")
def history():

    if not closed_trades:
        rows = "<tr><td>No closed trades</td></tr>"
    else:
        rows = ""
        for p in closed_trades:
            rows += f"""
            <tr>
            <td>{p['symbol']}</td>
            <td>{p['action']}</td>
            <td>{p['entry']}</td>
            <td>{p['pnl']}</td>
            <td>{p['status']}</td>
            </tr>
            """

    return f"<h2>Closed Trades</h2><table border=1>{rows}</table>"

# ============================================
# STATUS
# ============================================

@app.route("/status")
def status():
    return jsonify({
        "trades": trade_count,
        "open_positions": open_positions,
        "daily_pnl": daily_pnl
    })

# ============================================
# ROOT
# ============================================

@app.route("/")
def home():
    return "Trading Bot Running"

@app.route("/start")
def start_trading():
    global TRADING_ENABLED
    TRADING_ENABLED = True
    return "Trading Enabled"


@app.route("/stop")
def stop_trading():
    global TRADING_ENABLED
    TRADING_ENABLED = False
    return "Trading Disabled"

# ============================================
# RUN
# ============================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)