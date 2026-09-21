import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

from binance.client import Client
from binance.exceptions import BinanceAPIException

# ------------------------------------------------------------------ CONFIG
API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_SECRET_KEY")
PROXY_URL = os.environ.get("PROXY_URL")
LIVE = os.environ.get("LIVE_TRADING", "false").strip().lower() == "true"
TESTNET = os.environ.get("BINANCE_TESTNET", "false").strip().lower() == "true"

COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
         "ADAUSDT", "DOGEUSDT", "BNBUSDT", "AVAXUSDT"]

LEVERAGE = 5                  # kam leverage = liquidation door
TRADE_AMOUNT_USDT = 5.0       # ek trade me max margin
RISK_PER_TRADE_PCT = 1.0      # SL lagne par balance ka max ~1% loss
MAX_OPEN_POSITIONS = 2
MAX_DAILY_LOSS_PCT = 3.0      # aaj ka loss itna ho jaye to naye trade band
COOLDOWN_HOURS = 3            # loss wale coin par dobara itni der trade nahi

INTERVAL = "15m"
SL_ATR = 1.5                  # stop-loss = 1.5 x ATR
TP_ATR = 3.0                  # take-profit = 3 x ATR  (risk:reward = 1:2)
TRAIL_ATR = 1.0               # 2R ke baad trailing distance
MIN_ATR_PCT = 0.30            # bohat sust market = fees kha jati hain, skip
MAX_ATR_PCT = 3.00            # bohat wild market = skip
MAX_HOLD_HOURS = 12           # itni der me profit na bane to band
FEE_BUFFER_PCT = 0.10         # breakeven SL me fees ka buffer (0.10%)


def log(*a):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}]", *a, flush=True)


# ------------------------------------------------------------- INDICATORS
def ema(values, period):
    k = 2 / (period + 1)
    out, e = [], values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, period=14):
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def atr(highs, lows, closes, period=14):
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def parse_klines(raw):
    """Sirf CLOSED candles (aakhri chal rahi candle hata di)."""
    raw = raw[:-1]
    return {
        "high": [float(k[2]) for k in raw],
        "low": [float(k[3]) for k in raw],
        "close": [float(k[4]) for k in raw],
        "vol": [float(k[5]) for k in raw],
    }


def analyse(symbol, client):
    k15 = parse_klines(client.futures_klines(symbol=symbol, interval=INTERVAL, limit=150))
    k1h = parse_klines(client.futures_klines(symbol=symbol, interval="1h", limit=100))
    c = k15["close"]
    e9, e21, e50 = ema(c, 9)[-1], ema(c, 21)[-1], ema(c, 50)[-1]
    h_e20, h_e50 = ema(k1h["close"], 20)[-1], ema(k1h["close"], 50)[-1]
    a = atr(k15["high"], k15["low"], c)
    vol_avg = sum(k15["vol"][-21:-1]) / 20
    return {
        "price": c[-1], "e9": e9, "e21": e21, "e50": e50,
        "h_up": h_e20 > h_e50, "h_down": h_e20 < h_e50,
        "rsi": rsi(c), "atr": a, "atr_pct": a / c[-1] * 100,
        "vol_ok": k15["vol"][-1] >= vol_avg,
    }


def entry_signal(m):
    """('BUY'|'SELL'|None, score, reason)"""
    if not (MIN_ATR_PCT <= m["atr_pct"] <= MAX_ATR_PCT):
        return None, 0, f"ATR {m['atr_pct']:.2f}% range se bahar"
    if not m["vol_ok"]:
        return None, 0, "volume kam"
    stretch = abs(m["price"] - m["e21"]) / m["atr"]
    if stretch > 1.2:
        return None, 0, "price EMA se bohat door (chase nahi karna)"
    strength = abs(m["e9"] - m["e50"]) / m["atr"]
    if (m["e9"] > m["e21"] > m["e50"] and m["price"] > m["e21"]
            and m["h_up"] and 50 <= m["rsi"] <= 66):
        return "BUY", strength, "uptrend 15m+1h, RSI theek"
    if (m["e9"] < m["e21"] < m["e50"] and m["price"] < m["e21"]
            and m["h_down"] and 34 <= m["rsi"] <= 50):
        return "SELL", strength, "downtrend 15m+1h, RSI theek"
    return None, 0, "trend/RSI match nahi"


def exit_reason(side, m, pnl_r, held_h):
    """Khuli position ke liye 'abhi nikal jao' wajah, warna None."""
    if side == "BUY" and m["e9"] < m["e21"] and m["price"] < m["e21"]:
        return "trend palat gaya (long)"
    if side == "SELL" and m["e9"] > m["e21"] and m["price"] > m["e21"]:
        return "trend palat gaya (short)"
    if held_h >= MAX_HOLD_HOURS and pnl_r < 0.5:
        return f"{held_h:.0f}h ho gaye, profit nahi bana"
    return None


# ----------------------------------------------------------- EXCHANGE HELPERS
def floor_step(value, step):
    step = Decimal(str(step))
    return (Decimal(str(value)) / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_tick(value, tick):
    tick = Decimal(str(tick))
    return (Decimal(str(value)) / tick).to_integral_value() * tick


def load_filters(client):
    info = client.futures_exchange_info()
    out = {}
    for s in info["symbols"]:
        if s["symbol"] not in COINS:
            continue
        f = {x["filterType"]: x for x in s["filters"]}
        out[s["symbol"]] = {
            "step": f["LOT_SIZE"]["stepSize"],
            "min_qty": float(f["LOT_SIZE"]["minQty"]),
            "tick": f["PRICE_FILTER"]["tickSize"],
            "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 5)),
        }
    return out


def get_positions(client):
    res = {}
    for p in client.futures_position_information():
        amt = float(p["positionAmt"])
        if amt != 0 and p["symbol"] in COINS:
            res[p["symbol"]] = {
                "amt": amt,
                "side": "BUY" if amt > 0 else "SELL",
                "entry": float(p["entryPrice"]),
                "mark": float(p["markPrice"]),
                "opened_ms": int(p.get("updateTime", 0)),
            }
    return res


def open_algo(client, symbol=None):
    try:
        return client.futures_get_open_algo_orders(symbol=symbol) if symbol \
            else client.futures_get_open_algo_orders()
    except Exception as e:
        log(f"algo orders fetch error: {e}")
        return []


def cancel_algo(client, o):
    try:
        client.futures_cancel_algo_order(symbol=o["symbol"], algoId=o["algoId"])
    except Exception as e:
        log(f"cancel error {o.get('symbol')}: {e}")


def place_conditional(client, symbol, close_side, order_type, price, tick):
    return client.futures_create_algo_order(
        symbol=symbol, side=close_side, type=order_type,
        triggerPrice=str(round_tick(price, tick)), closePosition="true",
        workingType="MARK_PRICE",
    )


def close_market(client, symbol, pos, why):
    log(f"CLOSE {symbol}: {why}")
    if not LIVE:
        return
    side = Client.SIDE_SELL if pos["side"] == "BUY" else Client.SIDE_BUY
    client.futures_create_order(symbol=symbol, side=side, type="MARKET",
                                quantity=abs(pos["amt"]), reduceOnly="true")
    for o in open_algo(client, symbol):
        cancel_algo(client, o)


def daily_stats(client):
    """Aaj (UTC) ka net PnL + har symbol ka haal ka loss - exchange se, koi file nahi."""
    now = datetime.now(timezone.utc)
    start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    inc = client.futures_income_history(startTime=start, limit=1000)
    net = sum(float(i["income"]) for i in inc
              if i["incomeType"] in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"))
    cutoff = (time.time() - COOLDOWN_HOURS * 3600) * 1000
    recent_loss = set()
    per_sym = {}
    for i in inc:
        if i["incomeType"] == "REALIZED_PNL" and int(i["time"]) >= cutoff:
            per_sym[i["symbol"]] = per_sym.get(i["symbol"], 0) + float(i["income"])
    for s, v in per_sym.items():
        if v < 0:
            recent_loss.add(s)
    return net, recent_loss


# ------------------------------------------------------------------- MANAGE
def manage_positions(client, positions, filters):
    for sym, pos in positions.items():
        m = analyse(sym, client)
        side, entry, mark = pos["side"], pos["entry"], pos["mark"]
        r_dist = SL_ATR * m["atr"]
        pnl = (mark - entry) if side == "BUY" else (entry - mark)
        pnl_r = pnl / r_dist if r_dist else 0
        held_h = (time.time() * 1000 - pos["opened_ms"]) / 3.6e6 if pos["opened_ms"] else 0
        log(f"{sym} {side} entry={entry} mark={mark} PnL={pnl_r:+.2f}R held={held_h:.1f}h")

        why = exit_reason(side, m, pnl_r, held_h)
        if why:
            close_market(client, sym, pos, why)
            continue

        # SL ko sirf tight karna hai (breakeven / trailing)
        new_sl = None
        if pnl_r >= 2.0:
            new_sl = mark - TRAIL_ATR * m["atr"] if side == "BUY" else mark + TRAIL_ATR * m["atr"]
        elif pnl_r >= 1.0:
            buf = entry * FEE_BUFFER_PCT / 100
            new_sl = entry + buf if side == "BUY" else entry - buf
        if new_sl is None:
            continue

        orders = open_algo(client, sym)
        sl_orders = [o for o in orders if o.get("orderType") == "STOP_MARKET"]
        cur_sl = float(sl_orders[0]["triggerPrice"]) if sl_orders else None
        better = cur_sl is None or (new_sl > cur_sl if side == "BUY" else new_sl < cur_sl)
        if not better:
            continue
        log(f"{sym}: SL move {cur_sl} -> {round(new_sl, 6)} (profit lock)")
        if not LIVE:
            continue
        close_side = Client.SIDE_SELL if side == "BUY" else Client.SIDE_BUY
        for o in sl_orders:
            cancel_algo(client, o)
        try:
            place_conditional(client, sym, close_side, "STOP_MARKET", new_sl, filters[sym]["tick"])
        except Exception as e:
            close_market(client, sym, pos, f"naya SL nahi laga ({e}) - safety exit")


# -------------------------------------------------------------------- ENTRY
def open_trade(client, sym, side, m, balance, filters):
    f = filters[sym]
    price = m["price"]
    sl_dist, tp_dist = SL_ATR * m["atr"], TP_ATR * m["atr"]

    risk_usdt = balance * RISK_PER_TRADE_PCT / 100
    notional_cap = TRADE_AMOUNT_USDT * LEVERAGE
    qty = min(risk_usdt / sl_dist, notional_cap / price)
    qty = float(floor_step(qty, f["step"]))
    if qty < f["min_qty"] or qty * price < f["min_notional"]:
        log(f"{sym}: skip - qty*price=${qty * price:.2f} < min ${f['min_notional']}"
            f" (TRADE_AMOUNT_USDT ya balance badhao)")
        return False

    sl = price - sl_dist if side == "BUY" else price + sl_dist
    tp = price + tp_dist if side == "BUY" else price - tp_dist
    log(f"{'LIVE' if LIVE else 'PAPER'} {side} {sym} qty={qty} ~${qty * price:.2f} "
        f"SL={sl:.6g} TP={tp:.6g} risk~${qty * sl_dist:.2f}")
    if not LIVE:
        return True

    try:
        client.futures_change_leverage(symbol=sym, leverage=LEVERAGE)
    except Exception as e:
        log(f"leverage: {e}")
    try:
        client.futures_change_margin_type(symbol=sym, marginType="ISOLATED")
    except BinanceAPIException:
        pass  # pehle se ISOLATED

    client.futures_create_order(symbol=sym, side=side, type="MARKET", quantity=qty)
    time.sleep(1)
    pos = get_positions(client).get(sym)
    if not pos:
        log(f"{sym}: order ke baad position nahi mili")
        return False
    entry = pos["entry"]
    sl = entry - sl_dist if side == "BUY" else entry + sl_dist
    tp = entry + tp_dist if side == "BUY" else entry - tp_dist
    close_side = Client.SIDE_SELL if side == "BUY" else Client.SIDE_BUY
    try:
        place_conditional(client, sym, close_side, "STOP_MARKET", sl, f["tick"])
    except Exception as e:
        close_market(client, sym, pos, f"STOP-LOSS nahi laga ({e}) - bina SL position nahi rakhni")
        return False
    try:
        place_conditional(client, sym, close_side, "TAKE_PROFIT_MARKET", tp, f["tick"])
    except Exception as e:
        log(f"{sym}: TP nahi laga ({e}); SL laga hua hai, bot trend-exit se sambhal lega")
    return True


# --------------------------------------------------------------------- MAIN
def make_client():
    params = {"timeout": 20}
    if PROXY_URL:
        params["proxies"] = {"http": PROXY_URL, "https": PROXY_URL}  # <- asal bug fix
    return Client(API_KEY, API_SECRET, testnet=TESTNET, requests_params=params)


def run_cycle(client):
    log(f"Mode: {'LIVE' if LIVE else 'PAPER (asli order nahi)'}"
        f"{' [TESTNET]' if TESTNET else ''}")
    filters = load_filters(client)

    balance = 0.0
    for b in client.futures_account_balance():
        if b["asset"] == "USDT":
            balance = float(b["balance"])
    log(f"USDT balance: {balance:.2f}")

    positions = get_positions(client)

    # orphan TP/SL saaf
    for o in open_algo(client):
        if o.get("symbol") in COINS and o["symbol"] not in positions and LIVE:
            log(f"orphan order cancel: {o['symbol']}")
            cancel_algo(client, o)

    manage_positions(client, positions, filters)
    positions = get_positions(client) if LIVE else positions

    net_today, loss_syms = daily_stats(client)
    log(f"Aaj ka net PnL (fees ke saath): {net_today:+.3f} USDT")
    if balance > 0 and net_today <= -balance * MAX_DAILY_LOSS_PCT / 100:
        log(f"DAILY LOSS LIMIT ({MAX_DAILY_LOSS_PCT}%) hit - aaj naye trade band")
        return
    if len(positions) >= MAX_OPEN_POSITIONS:
        log("Max positions khuli hain - naya trade nahi")
        return

    best = None
    for sym in COINS:
        if sym in positions or sym in loss_syms:
            continue
        try:
            m = analyse(sym, client)
        except Exception as e:
            log(f"{sym}: data error {e}")
            continue
        side, score, why = entry_signal(m)
        log(f"{sym}: {side or '-'} | RSI {m['rsi']:.0f} ATR {m['atr_pct']:.2f}% | {why}")
        if side and (best is None or score > best[2]):
            best = (sym, side, score, m)
        time.sleep(0.2)

    if best:
        open_trade(client, best[0], best[1], best[3], balance, filters)
    else:
        log("Koi acha setup nahi - trade nahi (ye theek hai, har waqt trade karna zaroori nahi)")


if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        log("BINANCE_API_KEY / BINANCE_SECRET_KEY secrets set nahi hain")
        sys.exit(1)
    try:
        run_cycle(make_client())
    except Exception as e:
        log(f"FATAL: {type(e).__name__}: {e}")
        sys.exit(1)   # ab GitHub run laal (fail) dikhayega, jhoota green nahi
