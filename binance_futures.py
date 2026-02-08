from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_socketio import SocketIO
from unicorn_binance_websocket_api import BinanceWebSocketApiManager
import threading
import logging
import json
import os
import time
import csv
import datetime

# --- CONFIGURATION ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', ping_timeout=30)

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logging.getLogger("unicorn_binance_websocket_api").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Files
CSV_FILE_PATH = f"binance_data_{datetime.datetime.now().strftime('%Y-%m-%d')}.csv"
CONFIG_FILE = "binance_config.json"

# --- PARAMETERS ---
MIN_VOLUME_24H = 40_000_000 # Specified in 1_000_000_000
MAX_VOLUME_24H = 16_000_000_000 # Specified in 16_000_000_000
PRICE_CHANGE_THRESHOLD = 0.1
VOLUME_BURST_THRESHOLD = 0 # Now in PERCENTS (0.5%)
TRADES_CHANGE_THRESHOLD = 0 # PERCENTS of trades change
MAX_PRICE = 30
MIN_PRICE = 0
MIN_24H_CHANGE = 0  # Negative values down to -99 (0 = filter disabled)
MAX_24H_CHANGE = 0  # Positive values up to +25 (0 = filter disabled)

# Global variables
contracts_state = {}
monitor_contracts = set()
ignore_contracts = set()
signal_history = []  # History storage (RAM)
state_lock = threading.Lock()
csv_lock = threading.Lock()
history_lock = threading.Lock() 

stats_lock = threading.Lock()
seen_contracts = set()
eligible_contracts = set()

# --- HELPER FUNCTIONS ---
def load_config():
    default_config = {"monitor": [], "ignore": []}
    if not os.path.exists(CONFIG_FILE): return default_config
    try:
        with open(CONFIG_FILE, "r", encoding='utf-8') as f: return json.load(f)
    except (json.JSONDecodeError, IOError, OSError) as e:
        logging.warning(f"Config load error: {e}")
        return default_config

def save_config():
    cfg = {"monitor": list(monitor_contracts), "ignore": list(ignore_contracts)}
    try:
        with open(CONFIG_FILE, "w", encoding='utf-8') as f: json.dump(cfg, f, indent=4)
    except Exception as e: logging.error(f"Config save error: {e}")

def init_csv():
    with csv_lock:
        if not os.path.exists(CSV_FILE_PATH):
            with open(CSV_FILE_PATH, "w", newline='', encoding='utf-8') as f:
                csv.writer(f).writerow(["Symbol", "Type", "Price", "Change%", "24h%", "Vol_Delta_USD", "Trades_Delta", "Vol_24h", "Trades_24h", "Time", "Message"])

def write_to_csv(data):
    try:
        with csv_lock, open(CSV_FILE_PATH, "a", newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(data)
    except (IOError, OSError) as e:
        logging.error(f"CSV error: {e}")

config = load_config()
monitor_contracts = set(config["monitor"])
ignore_contracts = set(config["ignore"])
init_csv()

# --- LOGIC ---
def emit_stats_snapshot():
    with stats_lock:
        seen_total = len(seen_contracts)
        eligible_total = len(eligible_contracts)
    return {
        "seen_total": seen_total,
        "eligible_total": eligible_total,
        "filtered_total": max(seen_total - eligible_total, 0),
        "mon_count": len(monitor_contracts),
        "ign_count": len(ignore_contracts),
        "ts": datetime.datetime.now().strftime("%H:%M:%S"),
    }


def start_stats_emitter():
    while True:
        try:
            socketio.emit('stats', emit_stats_snapshot())
        except Exception as e:
            logging.debug(f"Stats emit error: {e}")
        time.sleep(1)

def process_stream_data(stream_data):
    if not stream_data: return
    if isinstance(stream_data, str):
        try: data = json.loads(stream_data)
        except: return
    else: data = stream_data

    ticker_list = []
    if isinstance(data, list): ticker_list = data
    elif isinstance(data, dict):
        if 'data' in data and isinstance(data['data'], list): ticker_list = data['data']
        elif 'e' in data and data['e'] == '24hrTicker': ticker_list = [data]
        elif 'result' in data: return

    if not ticker_list: return
    process_tickers(ticker_list)

def process_tickers(tickers):
    global contracts_state, signal_history
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    events = []

    _mon = monitor_contracts
    _ign = ignore_contracts
    _min_v = MIN_VOLUME_24H
    _max_v = MAX_VOLUME_24H
    _max_p = MAX_PRICE
    _min_p = MIN_PRICE
    _p_thr = PRICE_CHANGE_THRESHOLD
    _v_thr = VOLUME_BURST_THRESHOLD
    _t_thr = TRADES_CHANGE_THRESHOLD
    _min_chg = MIN_24H_CHANGE
    _max_chg = MAX_24H_CHANGE

    for t in tickers:
        s = t.get('s')
        
        # --- FILTER ---
        if not s: continue
        if not s.endswith('USDT'): continue
        if '_' in s: continue
        # --------------

        with stats_lock:
            seen_contracts.add(s)

        if s in _ign: continue
        if _mon and s not in _mon: continue

        try:
            p = float(t['c']) # Last Price
            q = float(t['q']) # Quote Volume (24h)
            n = int(t['n'])   # Number of Trades (24h)
            p_chg = float(t['P']) # 24h Price Change Percent
        except (KeyError, ValueError, TypeError):
            continue

        if (_min_v > 0 and q < _min_v) or (_max_v > 0 and q > _max_v) or (_max_p > 0 and p > _max_p) or (_min_p > 0 and p < _min_p): continue
        if (_min_chg != 0 and p_chg < _min_chg) or (_max_chg != 0 and p_chg > _max_chg):
            continue

        with stats_lock:
            eligible_contracts.add(s)
        
        # Formatting for frontend (to avoid pulling formatting logic to JS)
        # Volume: if > 1M, show "120M", otherwise "500K"
        if q >= 1_000_000:
            vol_fmt = f"{q/1_000_000:.1f}M"
        else:
            vol_fmt = f"{q/1_000:.0f}K"
            
        # Trades: same logic
        if n >= 1_000_000:
            trades_fmt = f"{n/1_000_000:.1f}M"
        elif n >= 1_000:
            trades_fmt = f"{n/1_000:.1f}K"
        else:
            trades_fmt = str(n)

        with state_lock:
            state = contracts_state.get(s)
            if not state:
                contracts_state[s] = {'h': p, 'v': q, 'tn': n, 'c': 0}
                continue

            # PRICE UP (Incremental Growth Logic with Multiple Signals)
            # For large price jumps, generate multiple signals with _p_thr step
            # Example: if price grew 12% with 0.1% threshold, generate 120 signals
            
            prev_anchor = state['h']
            if p > prev_anchor:
                chg = ((p - prev_anchor) / prev_anchor) * 100
                
                if _p_thr > 0 and chg >= _p_thr:
                    # Calculate how many _p_thr "steps" occurred
                    num_signals = int(chg / _p_thr)
                    
                    # Generate multiple signals
                    for step in range(num_signals):
                        state['c'] += 1
                        # Incrementally update anchor for each step
                        step_anchor = prev_anchor * (1 + (_p_thr / 100) * (step + 1))
                        
                        evt = {
                            "type": "PRICE UP", "symbol": s, "price": step_anchor, 
                            "prev": prev_anchor * (1 + (_p_thr / 100) * step) if step > 0 else prev_anchor, 
                            "change": round(_p_thr, 2), 
                            "count": state['c'], "time": ts, "alert": state['c']>=3,
                            "vol_24h": vol_fmt, "trades": trades_fmt,
                            "24h_chg": p_chg
                        }
                        events.append(evt)
                        write_to_csv((
                            s, "PRICE UP", f"{step_anchor:.8f}".rstrip('0').rstrip('.'), round(_p_thr, 2), p_chg, 
                            0, 0, vol_fmt, trades_fmt, ts, 
                            f"Change: +{round(_p_thr, 2)}% (Step {step+1}/{num_signals})"
                        ))
                    
                    # Update anchor to final price
                    state['h'] = p

            # VOLUME UP
            prev_vol_anchor = state['v']
            if q < prev_vol_anchor:
                state['v'] = q
            else:
                if prev_vol_anchor > 0:
                     vol_chg = ((q - prev_vol_anchor) / prev_vol_anchor) * 100
                     
                     if _v_thr > 0 and vol_chg >= _v_thr:
                        delta_usd = q - prev_vol_anchor
                        
                        evt = {
                            "type": "VOLUME UP", "symbol": s, "price": p, "change": 0,
                            "vol_change_pct": round(vol_chg, 2),
                            "vol_delta": f"{int(delta_usd):,}".replace(",", " "), 
                            "time": ts, "alert": False,
                            "vol_24h": vol_fmt, "trades": trades_fmt,
                            "24h_chg": p_chg # New Field
                        }
                        events.append(evt)
                        write_to_csv((
                            s, "VOLUME UP", f"{p:.8f}".rstrip('0').rstrip('.'), 0, p_chg, 
                            int(delta_usd), 0, vol_fmt, trades_fmt, ts, 
                            f"Vol Chg: +{round(vol_chg, 2)}% (${int(delta_usd):,})".replace(",", " ")
                        ))
                        state['v'] = q
                else:
                    state['v'] = q

            # TRADES UP
            prev_trades_anchor = state.get('tn', 0)
            
            if n < prev_trades_anchor:
                state['tn'] = n
            else:
                if prev_trades_anchor > 0:
                    trades_chg = ((n - prev_trades_anchor) / prev_trades_anchor) * 100
                    if _t_thr > 0 and trades_chg >= _t_thr:
                         d_trades = n - prev_trades_anchor
                         evt = {
                            "type": "TRADES UP", "symbol": s, "price": p, "change": 0,
                            "trades_change_pct": round(trades_chg, 2),
                            "trades_delta": d_trades,
                            "time": ts, "alert": False,
                            "vol_24h": vol_fmt, "trades": trades_fmt,
                            "24h_chg": p_chg # New Field
                         }
                         events.append(evt)
                         write_to_csv((
                            s, "TRADES UP", f"{p:.8f}".rstrip('0').rstrip('.'), 0, p_chg, 
                            0, d_trades, vol_fmt, trades_fmt, ts, 
                            f"Trades Chg: +{round(trades_chg, 2)}% (+{d_trades})"
                         ))
                         state['tn'] = n # update anchor
                else:
                    state['tn'] = n

    if events:
        with history_lock:
            for e in events:
                signal_history.insert(0, e)
                socketio.emit('new_signal', e)
                if e['alert']:
                    socketio.emit('alert', {'symbol': e['symbol'], 'msg': f"PUMP {e['count']}x"})
            



def start_background_task():
    logging.info("Connecting to Binance WebSocket...")
    manager = BinanceWebSocketApiManager(exchange="binance.com-futures", output_default="raw_data")
    stream_id = manager.create_stream(channels=['arr'], markets=['!ticker'], process_stream_data=process_stream_data)
    
    while True:
        if manager.is_manager_stopping(): break
        try:
            info = manager.get_stream_info(stream_id)
            status = info.get('status', 'unknown') if info else 'unknown'
            if status != 'running' and status != 'unknown':
                socketio.emit('connection_status', {'status': 'error', 'msg': f"Stream Status: {status}"})
            else:
                socketio.emit('connection_status', {'status': 'ok'})
        except Exception as e:
            logging.debug(f"Stream status check error: {e}")
        time.sleep(5)

# --- ROUTES ---
@app.route('/')
def index():
    return render_template('index_binance.html', 
                           min_vol=MIN_VOLUME_24H, 
                           max_vol=MAX_VOLUME_24H,
                           max_price=MAX_PRICE,
                           min_price=MIN_PRICE,
                           min_24h_chg=MIN_24H_CHANGE,
                           max_24h_chg=MAX_24H_CHANGE,
                           price_thr=PRICE_CHANGE_THRESHOLD,
                           vol_burst=VOLUME_BURST_THRESHOLD,
                           trades_thr=TRADES_CHANGE_THRESHOLD,
                           monitors=sorted(list(monitor_contracts)),
                           ignores=sorted(list(ignore_contracts)),
                           history=signal_history) 

@app.route('/update_params', methods=['POST'])
def update_params():
    global MIN_VOLUME_24H, MAX_VOLUME_24H, MAX_PRICE, MIN_PRICE, PRICE_CHANGE_THRESHOLD, VOLUME_BURST_THRESHOLD, TRADES_CHANGE_THRESHOLD, MIN_24H_CHANGE, MAX_24H_CHANGE
    def parse_val(val, type_func=float, default=0):
        if not val: return type_func(default)
        # Remove spaces and replace commas with dots for decimal numbers
        cleaned = "".join(str(val).split()).replace(',', '.')
        try:
            return type_func(cleaned)
        except (ValueError, TypeError):
            return type_func(default)

    try:
        MIN_VOLUME_24H = parse_val(request.form.get('min_vol'), int, default=0)
        MAX_VOLUME_24H = parse_val(request.form.get('max_vol'), int, default=100_000_000_000)
        MAX_PRICE = parse_val(request.form.get('max_price'), float, default=1_000_000)
        MIN_PRICE = parse_val(request.form.get('min_price'), float, default=0)
        MIN_24H_CHANGE = max(parse_val(request.form.get('min_24h_chg'), float, default=0), -99)
        MAX_24H_CHANGE = parse_val(request.form.get('max_24h_chg'), float, default=0)  # No upper limit
        PRICE_CHANGE_THRESHOLD = parse_val(request.form.get('price_thr'), float, default=0)
        VOLUME_BURST_THRESHOLD = parse_val(request.form.get('vol_burst'), float, default=0)
        TRADES_CHANGE_THRESHOLD = parse_val(request.form.get('trades_thr'), float, default=0)
    except Exception as e:
        logging.warning(f"Param update error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 400

    logging.info(
        "Params updated: PriceThreshold=%s, VolBurst=%s, MinVol=%s, MaxVol=%s, MinPrice=%s, MaxPrice=%s, Min24h%%=%s, Max24h%%=%s",
        PRICE_CHANGE_THRESHOLD,
        VOLUME_BURST_THRESHOLD,
        MIN_VOLUME_24H,
        MAX_VOLUME_24H,
        MIN_PRICE,
        MAX_PRICE,
        MIN_24H_CHANGE,
        MAX_24H_CHANGE,
    )
    return jsonify({"status": "success"})


@app.route('/clear_session', methods=['POST'])
def clear_session():
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with history_lock:
        signal_history.clear()

    with state_lock:
        for s, st in contracts_state.items():
            if isinstance(st, dict):
                st['c'] = 0

    with stats_lock:
        seen_contracts.clear()
        eligible_contracts.clear()

    write_to_csv(["", "SESSION", "", "", "", "", "", "", "", ts, "---------------- New session ----------------"])
    return jsonify({"status": "success"})

@app.route('/manage_list', methods=['POST'])
def manage_list():
    act = request.form.get('action')
    sym = request.form.get('symbol', '').upper().strip()
    if sym:
        if act == 'add_mon': monitor_contracts.add(sym); ignore_contracts.discard(sym)
        elif act == 'rem_mon': monitor_contracts.discard(sym)
        elif act == 'add_ign': ignore_contracts.add(sym); monitor_contracts.discard(sym)
        elif act == 'rem_ign': ignore_contracts.discard(sym)
        save_config()
    return redirect(url_for('index'))

if __name__ == '__main__':
    t = threading.Thread(target=start_background_task, daemon=True)
    t.start()
    st = threading.Thread(target=start_stats_emitter, daemon=True)
    st.start()
    logging.info("Server starting on port 5000...")
    socketio.run(app, host='0.0.0.0', port=5000, debug=True, use_reloader=False, allow_unsafe_werkzeug=True)
