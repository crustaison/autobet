#!/usr/bin/env python3
"""
autobet_main.py — Unified Autobet Server v2
Multi-venue prediction market platform: Kalshi + Polymarket, auth, onboarding,
risk engine, paper runs, audit log, chat, tooltips.
Port 7778. Reads credentials from ~/autoresearch/.env
"""

import http.server
import socketserver
import json
import sqlite3
import threading
import time
import os
import sys
import base64
import hashlib
import hmac
import secrets
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
import traceback

def _get_tz():
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo("America/Chicago")
    except Exception:
        pass
    try:
        import pytz
        return pytz.timezone("America/Chicago")
    except Exception:
        pass
    # fallback CDT = UTC-5 (April–October)
    return timezone(timedelta(hours=-5))

_TZ = _get_tz()

def now_cst():
    return datetime.now(_TZ)

def ts_cst(ts):
    return datetime.fromtimestamp(ts, tz=_TZ)

def tz_label():
    try:
        return now_cst().strftime("%Z")
    except:
        return "CT"

# ── Configuration ──────────────────────────────────────────────────────────────
BASE_DIR   = Path.home() / "autoresearch"
DATA_DIR   = BASE_DIR / "data"
DB_PATH    = Path.home() / "autobet" / "data" / "autobet.db"
ENV_FILE   = BASE_DIR / ".env"
KALSHI_PEM = Path.home() / "autobet" / "kalshi.key"
LOGO_PATH  = Path.home() / "autobet" / "logo.jpg"

PORT = 7778
COINS = ["BTC", "XRP", "SOL", "ETH", "DOGE", "BNB", "HYPE"]
SERIES = {"BTC": "KXBTC15M", "XRP": "KXXRP15M", "SOL": "KXSOL15M", "ETH": "KXETH15M",
          "DOGE": "KXDOGE15M", "BNB": "KXBNB15M", "HYPE": "KXHYPE15M"}
COIN_COLORS  = {"BTC": "#f7931a", "XRP": "#0066cc", "SOL": "#9945ff", "ETH": "#627eea",
                "DOGE": "#c2a633", "BNB": "#f3ba2f", "HYPE": "#00e5ff"}
COIN_LETTERS = {"BTC": "B", "XRP": "X", "SOL": "S", "ETH": "E",
                "DOGE": "D", "BNB": "N", "HYPE": "H"}
# Coins not on Coinbase — use alternate price source
COIN_PRICE_OVERRIDE = {"HYPE": "https://api.coingecko.com/api/v3/simple/price?ids=hyperliquid&vs_currencies=usd"}

KALSHI_BASE   = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_BASE = "https://clob.polymarket.com"
COINBASE_URL  = "https://api.coinbase.com/v2/prices/{}-USD/spot"
MINIMAX_URL   = "https://api.minimax.io/anthropic/v1/messages"

STARTING_CAPITAL = 500.0
TRADE_SIZE       = 20.0
KALSHI_FEE_RATE  = 0.07
MAX_CONTRACTS    = 500    # Realistic Kalshi order book depth at any price
ENTRY_FLOOR      = 0.05   # Below this = lottery ticket; liquidity impossible
ENTRY_CEILING    = 0.80   # Above this = negative EV confirmed across all coins (data: 0.8+ entry = -$1,886 net)

# ── Load environment ────────────────────────────────────────────────────────────
def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, _, v = line.partition('=')
                env[k.strip()] = v.strip()
    for k in ["MINIMAX_API_KEY", "MINIMAX_MODEL", "KALSHI_KEY_ID", "AUTOBET_SECRET"]:
        if k in os.environ:
            env[k] = os.environ[k]
    return env

ENV = load_env()
MINIMAX_KEY   = ENV.get("MINIMAX_API_KEY", "")
_MINIMAX_MODEL_DEFAULT = ENV.get("MINIMAX_MODEL", "MiniMax-M2.5")
KALSHI_KEY_ID = ENV.get("KALSHI_KEY_ID", "a7614a86-cb1e-4bd3-8c54-835046d28c21")

def get_minimax_model():
    """Read the active model from the settings table (falls back to .env default)."""
    try:
        conn = db_connect()
        row = conn.execute("SELECT value FROM settings WHERE key='model'").fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return _MINIMAX_MODEL_DEFAULT

# Keep MINIMAX_MODEL as a property-like alias for startup use only.
# Always call get_minimax_model() at decision/display time.
MINIMAX_MODEL = _MINIMAX_MODEL_DEFAULT
def _load_session_secret():
    secret = ENV.get("AUTOBET_SECRET", "")
    if secret:
        return secret
    # Persist secret in DB so restarts don't invalidate sessions
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM _meta WHERE key='session_secret'").fetchone()
        if row:
            conn.close()
            return row[0]
        new_secret = secrets.token_hex(32)
        conn.execute("INSERT INTO _meta (key, value) VALUES ('session_secret', ?)", (new_secret,))
        conn.commit()
        conn.close()
        return new_secret
    except Exception:
        return secrets.token_hex(32)
SESSION_SECRET = _load_session_secret()

# ── Kalshi RSA auth ─────────────────────────────────────────────────────────────
def _load_pem_key():
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding as apad
        return serialization.load_pem_private_key(KALSHI_PEM.read_bytes(), password=None)
    except Exception as e:
        print(f"[AUTH] PEM load error: {e}")
        return None

_PEM_KEY = None
_PEM_LOCK = threading.Lock()

def get_pem_key():
    global _PEM_KEY
    with _PEM_LOCK:
        if _PEM_KEY is None:
            _PEM_KEY = _load_pem_key()
    return _PEM_KEY

def kalshi_auth_headers(method, path):
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding as apad
        key = get_pem_key()
        if key is None:
            return {}
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path).encode()
        sig = key.sign(msg, apad.PSS(
            mgf=apad.MGF1(hashes.SHA256()),
            salt_length=apad.PSS.DIGEST_LENGTH
        ), hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }
    except Exception as e:
        print(f"[AUTH] Sign error: {e}")
        return {}

def kalshi_get(path, params=None):
    url = KALSHI_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    hdrs = kalshi_auth_headers("GET", "/trade-api/v2" + path)
    hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[KALSHI] GET {path}: {e}")
        return None

def kalshi_post(path, body_dict):
    url = KALSHI_BASE + path
    auth_path = "/trade-api/v2" + path
    hdrs = kalshi_auth_headers("POST", auth_path)
    hdrs["Content-Type"] = "application/json"
    data = json.dumps(body_dict).encode()
    print(f"[KALSHI POST] key={KALSHI_KEY_ID[:8]} path={auth_path} body={data.decode()[:120]}")
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        print(f"[KALSHI] POST {path} HTTP {e.code}: {err_body}")
        return None, f"HTTP {e.code}: {err_body}"
    except Exception as e:
        print(f"[KALSHI] POST {path}: {e}")
        return None, str(e)

def place_kalshi_order(coin, ticker, direction, contracts, entry):
    """
    Place a limit order on Kalshi.
    Fetches a fresh ticker from the API to avoid using stale cached tickers.
    Returns (order_id, actual_ticker_used, error_str)
    """
    # Always get the freshest open ticker — never trust cached value
    series = SERIES.get(coin, f"KX{coin}15M")
    fresh = kalshi_get("/markets", {"series_ticker": series, "status": "open", "limit": 1})
    fresh_mkts = (fresh or {}).get("markets", [])
    if fresh_mkts:
        live_ticker = fresh_mkts[0]["ticker"]
    else:
        live_ticker = ticker  # fallback to cached
    if not live_ticker:
        return None, ticker, "No open market found"

    side = "yes" if direction == "YES" else "no"
    limit_price = max(1, min(99, round(entry * 100)))
    body = {
        "ticker": live_ticker,
        "client_order_id": f"autobet_{live_ticker}_{int(time.time())}",
        "type": "limit",
        "action": "buy",
        "side": side,
        "count": int(contracts),
        "yes_price": limit_price if direction == "YES" else (100 - limit_price),
    }
    resp, err = kalshi_post("/portfolio/orders", body)
    if err:
        return None, live_ticker, err
    order_id = (resp or {}).get("order", {}).get("order_id")
    return order_id, live_ticker, None

def sell_kalshi_position(ticker, direction, contracts, exit_price):
    """
    Sell (exit) an existing Kalshi position before settlement.
    Selling NO = placing a sell order on the no side.
    exit_price: the current market bid for our side (what we'd receive per contract).
    Returns (order_id, error_str)
    """
    side = "yes" if direction == "YES" else "no"
    # Sell at market bid minus 1¢ to ensure fill
    limit_price = max(1, min(99, round(exit_price * 100)))
    body = {
        "ticker": ticker,
        "client_order_id": f"autobet_exit_{ticker}_{int(time.time())}",
        "type": "limit",
        "action": "sell",
        "side": side,
        "count": int(contracts),
        "yes_price": limit_price if direction == "YES" else (100 - limit_price),
    }
    resp, err = kalshi_post("/portfolio/orders", body)
    if err:
        return None, err
    order_id = (resp or {}).get("order", {}).get("order_id")
    return order_id, None

def check_exit_positions():
    """
    Evaluate active live positions for early exit.
    Called every 60s from sync thread.
    Rules (configurable in settings):
      - Take profit:  unrealized >= take_profit_pct% of stake
      - Stop loss:    unrealized <= -stop_loss_pct% of stake
      - Time cliff:   secs_left <= time_cliff_secs and any unrealized profit
      - LLM check:    at ~midpoint (secs_left ~450), ask LLM hold/sell
    """
    try:
        conn = db_connect()
        # Load thresholds from settings
        def _setting(key, default):
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return float(row[0]) if row else default
        take_profit_pct = _setting("exit_take_profit_pct", 40.0)
        stop_loss_pct   = _setting("exit_stop_loss_pct",   65.0)
        time_cliff_secs = _setting("exit_time_cliff_secs", 90.0)
        llm_exit_enabled = _setting("exit_llm_check", 1.0)

        now = int(time.time())
        wts = kalshi_window_ts()
        secs_left = max(0, wts + 900 - now)

        # Get filled live orders for current window not yet exited
        positions = conn.execute("""
            SELECT id, coin, ticker, direction, filled_contracts, avg_fill_price
            FROM live_orders
            WHERE window_ts = ? AND status IN ('filled','executed','filled_partial')
              AND filled_contracts > 0 AND avg_fill_price > 0
              AND exit_at IS NULL
        """, (wts,)).fetchall()
        conn.close()

        if not positions:
            return

        with _state_lock:
            mkts = {k: dict(v) for k, v in _active_mkts.items()}

        for pos in positions:
            lo_id, coin, ticker, direction, n_filled, avg_price = pos
            n = int(n_filled)
            avg_p = float(avg_price)
            mkt = mkts.get(coin, {})
            yes_bid = float(mkt.get("yes_bid") or 0)
            yes_ask = float(mkt.get("yes_ask") or 0)
            if not yes_bid or not yes_ask:
                continue

            # Current exit value and unrealized P&L
            if direction == "NO":
                cur_bid = 1.0 - yes_ask   # what we'd get selling NO right now (bid side)
                cur_val = 1.0 - yes_bid   # mid value
            else:
                cur_bid = yes_bid
                cur_val = yes_bid

            stake   = n * avg_p
            unreal  = n * (cur_val - avg_p)
            unreal_pct = (unreal / stake * 100) if stake > 0 else 0

            reason = None

            # Rule 1: Trailing stop — track peak unrealized P&L, exit if fallen X% from peak
            peak_key = f"trailing_peak_{lo_id}"
            try:
                conn_tp = db_connect()
                peak_row = conn_tp.execute(
                    "SELECT value FROM settings WHERE key=?", (peak_key,)
                ).fetchone()
                peak_pct = float(peak_row[0]) if peak_row else unreal_pct
                if unreal_pct > peak_pct:
                    peak_pct = unreal_pct
                    conn_tp.execute(
                        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?,?,?)",
                        (peak_key, str(peak_pct), now_cst().isoformat())
                    )
                    conn_tp.commit()
                conn_tp.close()
            except Exception:
                peak_pct = unreal_pct

            # Trailing stop: if was up >= take_profit_pct and has fallen > 15% from peak, exit
            trail_drop = peak_pct - unreal_pct
            if peak_pct >= take_profit_pct and trail_drop >= 15.0:
                reason = f"trailing_stop (peak {peak_pct:+.0f}% → now {unreal_pct:+.0f}%, dropped {trail_drop:.0f}%)"
            elif unreal_pct >= take_profit_pct and secs_left <= 120:
                # Lock in profit in last 2 minutes if at target
                reason = f"take_profit_lock ({unreal_pct:+.0f}% >= {take_profit_pct:.0f}%, {secs_left}s left)"

            # Rule 2: Stop loss
            elif unreal_pct <= -stop_loss_pct:
                reason = f"stop_loss ({unreal_pct:+.0f}% <= -{stop_loss_pct:.0f}%)"

            # Rule 3: Time cliff — exit any winning position in last N seconds
            elif secs_left <= time_cliff_secs and unreal > 0:
                reason = f"time_cliff ({secs_left}s left, up {unreal_pct:+.0f}%)"

            # Rule 4: LLM mid-window check (once, around secs_left=430-470)
            elif llm_exit_enabled and 430 <= secs_left <= 470 and abs(unreal_pct) > 5:
                reason = _llm_exit_check(coin, direction, n, avg_p, cur_val, unreal, secs_left, mkt)

            if reason:
                # Place sell order
                order_id, err = sell_kalshi_position(ticker, direction, n, cur_bid)
                exit_pnl = round(n * (cur_bid - avg_p), 4)
                conn2 = db_connect()
                if order_id:
                    conn2.execute("""
                        UPDATE live_orders SET exit_price=?, exit_at=?, exit_reason=?, status='exited'
                        WHERE id=?
                    """, (round(cur_bid, 4), now_cst().isoformat(), reason, lo_id))
                    conn2.commit()
                    print(f"[EXIT] {coin} {direction} {n}c: {reason} | exit@{cur_bid:.3f} entry@{avg_p:.3f} pnl={exit_pnl:+.2f}")
                    audit("position_exited", "live_orders", str(lo_id),
                          {"coin": coin, "direction": direction, "contracts": n,
                           "entry": avg_p, "exit": cur_bid, "pnl": exit_pnl, "reason": reason})
                else:
                    print(f"[EXIT] {coin}: sell order failed — {err}")
                conn2.close()
    except Exception as e:
        print(f"[EXIT CHECK] {e}")

def _llm_exit_check(coin, direction, n, avg_price, cur_val, unreal, secs_left, mkt):
    """
    Ask the dual-LLM whether to hold or exit the current position.
    Returns an exit reason string if it recommends selling, None to hold.
    """
    try:
        yes_bid = float(mkt.get("yes_bid") or 0)
        yes_ask = float(mkt.get("yes_ask") or 0)
        mid = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else 0
        unreal_pct = (unreal / (n * avg_price) * 100) if avg_price > 0 else 0

        prompt = f"""You are managing an active position in a 15-minute {coin} prediction market contract.

Position: {direction} {n} contracts, entered at {avg_price:.2f} ({avg_price*100:.0f}¢)
Current market: yes_bid={yes_bid:.3f} yes_ask={yes_ask:.3f} mid={mid:.3f}
Current {direction} value: {cur_val:.3f} ({cur_val*100:.0f}¢)
Unrealized P&L: {unreal:+.2f} ({unreal_pct:+.0f}% of stake)
Time remaining: {secs_left}s (~{secs_left//60}m {secs_left%60}s)

Should you hold this position to settlement or sell now to lock in the current value?
Consider: remaining time, market momentum, probability of improvement vs reversal.

Respond with JSON only:
{{"action": "hold" or "sell", "rationale": "one line reason"}}"""

        import threading as _t
        results = {}
        def _mm():
            try:
                import urllib.request as _ur
                payload = json.dumps({"model": get_minimax_model(), "max_tokens": 256,
                                      "messages": [{"role": "user", "content": prompt}]}).encode()
                req = _ur.Request(MINIMAX_URL, data=payload,
                                  headers={"Content-Type": "application/json",
                                           "x-api-key": MINIMAX_KEY, "anthropic-version": "2023-06-01"})
                with _ur.urlopen(req, timeout=25) as r:
                    resp = json.loads(r.read())
                    for block in resp.get("content", []):
                        if block.get("type") == "text":
                            text = block["text"].strip()
                            if "```" in text:
                                text = text.split("```")[1].lstrip("json").strip()
                            results["mm"] = json.loads(text)
                            break
            except Exception:
                pass

        t = _t.Thread(target=_mm, daemon=True)
        t.start()
        t.join(timeout=28)

        r = results.get("mm")
        if r and r.get("action") == "sell":
            print(f"[EXIT LLM] {coin}: sell recommended — {r.get('rationale','')[:80]}")
            return f"llm_sell ({r.get('rationale','')[:60]})"
        elif r:
            print(f"[EXIT LLM] {coin}: hold recommended — {r.get('rationale','')[:80]}")
    except Exception as e:
        print(f"[EXIT LLM] {coin}: {e}")
    return None

def sync_live_orders():
    """
    Poll Kalshi for status of placed orders and sync actual fill/P&L into live_orders.
    Runs in background every 60s. Updates status to 'filled'/'canceled' and records actual fill price.
    """
    try:
        conn = db_connect()
        pending = conn.execute(
            "SELECT id, coin, window_ts, ticker, direction, contracts, order_id "
            "FROM live_orders WHERE status IN ('placed','filled','filled_partial','executed') "
            "AND order_id IS NOT NULL AND (filled_contracts IS NULL OR filled_contracts = 0)"
        ).fetchall()
        conn.close()
        for row in pending:
            lo_id, coin, wts, ticker, direction, contracts, order_id = row
            resp = kalshi_get(f"/portfolio/orders/{order_id}")
            if not resp:
                continue
            order = resp.get("order", {})
            status = order.get("status", "")
            # Kalshi v2 API uses fill_count_fp (string) and {side}_price_dollars (already in dollars)
            filled_count = float(order.get("fill_count_fp") or order.get("contracts_filled") or 0)
            side = order.get("side", direction.lower())  # "yes" or "no"
            price_key = f"{side}_price_dollars"
            avg_price = float(order.get(price_key) or order.get("avg_fill_price") or 0)
            # avg_fill_price legacy field was in cents; price_dollars fields are already dollars
            if price_key not in order and avg_price > 1:
                avg_price = avg_price / 100.0
            new_status = {"filled": "filled", "executed": "filled", "canceled": "canceled",
                          "resting": "placed", "partially_filled": "filled_partial"}.get(status, status)
            # Detect partial fill when Kalshi reports "filled" but count < requested
            filled_count = int(filled_count)
            if new_status == "filled" and filled_count > 0 and filled_count < int(contracts or 0):
                new_status = "filled_partial"
            conn2 = db_connect()
            conn2.execute(
                "UPDATE live_orders SET status=?, filled_contracts=?, avg_fill_price=? WHERE id=?",
                (new_status, filled_count, avg_price, lo_id)
            )
            conn2.commit()
            conn2.close()
            if new_status in ("filled", "filled_partial") and filled_count > 0 and avg_price > 0:
                fill_note = " (partial)" if new_status == "filled_partial" else ""
                print(f"[LIVE] {coin} order {order_id[:12]} filled{fill_note}: {filled_count} contracts @ {avg_price:.3f}")
    except Exception as e:
        print(f"[LIVE SYNC] {e}")

def sync_kalshi_balance():
    """
    For any coin in live mode, pull the real Kalshi portfolio balance
    and overwrite the paper_accounts capital with the actual value.
    Kalshi has one balance shared across all coins — we split it proportionally
    by starting capital, then reconcile each live coin's account.
    Since we only have one Kalshi account, we just set each live coin's
    paper balance to: starting_capital + (kalshi_total - sum_of_all_starting_capitals).
    Simpler: track the delta per coin from live orders and apply to paper account.
    Actually simplest: just set the live coin's paper balance = Kalshi balance,
    since there's only one live coin at a time.
    """
    try:
        resp = kalshi_get("/portfolio/balance")
        if not resp:
            return
        kalshi_bal = resp.get("balance", 0) / 100.0  # cents -> dollars
        conn = db_connect()
        # Find all live-mode coins
        live_coins = [r[0] for r in conn.execute(
            "SELECT coin FROM coin_modes WHERE mode='live'"
        ).fetchall()]
        global_live = conn.execute("SELECT global_live_enabled FROM system_state WHERE id=1").fetchone()
        if not (global_live and global_live[0]) or not live_coins:
            conn.close()
            return
        # Always write pool_balance = real Kalshi balance
        conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('pool_balance',?,?)",
                     (str(round(kalshi_bal, 2)), now_cst().isoformat()))
        conn.commit()
        # If exactly one live coin and not pool mode, sync its paper balance too
        pool_row = conn.execute("SELECT value FROM settings WHERE key='pool_mode'").fetchone()
        pool_on = pool_row and pool_row[0] == "1"
        if not pool_on and len(live_coins) == 1:
            coin = live_coins[0]
            old = conn.execute("SELECT capital FROM paper_accounts WHERE coin=?", (coin,)).fetchone()
            old_bal = old[0] if old else 0
            if abs(old_bal - kalshi_bal) > 0.01:
                conn.execute("UPDATE paper_accounts SET capital=?, updated_at=? WHERE coin=?",
                             (kalshi_bal, now_cst().isoformat(), coin))
                conn.execute("UPDATE paper_runs SET current_capital=? WHERE coin=? AND status='active'",
                             (kalshi_bal, coin))
                conn.commit()
                print(f"[BALANCE SYNC] {coin} paper balance {old_bal:.2f} -> {kalshi_bal:.2f} (Kalshi)")
        conn.close()
    except Exception as e:
        print(f"[BALANCE SYNC] {e}")

def live_order_sync_loop():
    time.sleep(30)
    while True:
        try:
            sync_live_orders()
            sync_kalshi_balance()
            check_exit_positions()
        except Exception as e:
            print(f"[LIVE SYNC LOOP] {e}")
        time.sleep(60)

# ── Kalshi order book liquidity ─────────────────────────────────────────────────
_ob_cache = {}   # ticker -> (fetched_ts, orderbook_fp dict)
_ob_lock  = threading.Lock()
_pool_last_placed_wts = 0   # prevents duplicate pool orders in the same window
# (local LLM semaphore removed — dual MiniMax replaces local 35B subprocess)

def fetch_orderbook(ticker):
    """Return orderbook_fp dict for ticker, cached 45s. Returns None on error."""
    with _ob_lock:
        cached = _ob_cache.get(ticker)
        if cached and time.time() - cached[0] < 45:
            return cached[1]
    data = kalshi_get(f"/markets/{ticker}/orderbook")
    if not data:
        return None
    ob = data.get("orderbook_fp") or data.get("orderbook", {})
    with _ob_lock:
        _ob_cache[ticker] = (time.time(), ob)
    return ob

def get_available_contracts(ticker, direction, entry):
    """Return available contracts at entry price from live order book.
    direction=YES: sum yes_dollars qty where ask_price <= entry
    direction=NO:  sum no_dollars qty where ask_price <= entry
    Returns (available, ob_fetched). available=None means API unavailable."""
    ob = fetch_orderbook(ticker)
    if ob is None:
        return None, False
    side = "yes_dollars" if direction == "YES" else "no_dollars"
    levels = ob.get(side, [])
    available = 0.0
    for price_s, qty_s in levels:
        try:
            if float(price_s) <= entry + 0.005:   # 0.5-cent tolerance for rounding
                available += float(qty_s)
        except (ValueError, TypeError):
            continue
    return available, True

def fetch_kalshi_comments(ticker, coin):
    """Fetch recent market comments from Kalshi and cache in _kalshi_comments."""
    try:
        data = kalshi_get(f"/markets/{ticker}/comments?limit=20")
        if not data:
            return
        comments = data.get("comments", [])
        snippets = []
        for c in comments:
            text = (c.get("content") or c.get("body") or "").strip()
            if text and len(text) > 5:
                snippets.append(text[:120])
        with _state_lock:
            _kalshi_comments[coin] = snippets[:10]
    except Exception:
        pass

def fetch_kalshi_order_book(ticker):
    """Return a human-readable order book depth summary for the LLM prompt."""
    try:
        ob = fetch_orderbook(ticker)
        if not ob:
            return None
        yes_levels = ob.get("yes_dollars", [])
        no_levels  = ob.get("no_dollars", [])
        # Top 3 YES asks and NO asks (sorted by price ascending = cheapest first)
        def top3(levels):
            try:
                srt = sorted(levels, key=lambda x: float(x[0]))[:3]
                return " | ".join(f"{float(p):.2f}x{float(q):.0f}" for p, q in srt)
            except Exception:
                return "n/a"
        yes_str = top3(yes_levels)
        no_str  = top3(no_levels)
        total_yes = sum(float(q) for _, q in yes_levels) if yes_levels else 0
        total_no  = sum(float(q) for _, q in no_levels) if no_levels else 0
        return (f"Order book depth: YES ask ladder (price×qty): {yes_str}  total={total_yes:.0f}c | "
                f"NO ask ladder: {no_str}  total={total_no:.0f}c")
    except Exception:
        return None

# ── Database ────────────────────────────────────────────────────────────────────
def db_connect():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def db_migrate(conn):
    """Drop or alter Clyde-era tables that have incompatible schemas."""
    # system_state: Clyde used key/value, we need single-row with specific columns
    cols = [r[1] for r in conn.execute("PRAGMA table_info(system_state)").fetchall()]
    if cols and "onboarding_complete" not in cols:
        conn.execute("DROP TABLE system_state")
        print("[DB] Dropped incompatible system_state table")
    # paper_runs: check for our columns
    cols = [r[1] for r in conn.execute("PRAGMA table_info(paper_runs)").fetchall()]
    if cols and "reset_reason" not in cols:
        conn.execute("DROP TABLE paper_runs")
        print("[DB] Dropped incompatible paper_runs table")
    # users: may be missing updated_at column
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if cols and "updated_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN updated_at TEXT")
        print("[DB] Added updated_at to users table")
    # paper_trades: add run_id if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(paper_trades)").fetchall()]
    if cols and "run_id" not in cols:
        conn.execute("ALTER TABLE paper_trades ADD COLUMN run_id INTEGER")
        print("[DB] Added run_id to paper_trades")

    # live_orders: add settlement + exit columns if missing
    lo_cols = [r[1] for r in conn.execute("PRAGMA table_info(live_orders)").fetchall()]
    if lo_cols:
        for col, typedef in [
            ("pnl", "REAL"), ("actual_direction", "TEXT"), ("resolved_at", "TEXT"),
            ("exit_price", "REAL"), ("exit_at", "TEXT"), ("exit_reason", "TEXT"),
        ]:
            if col not in lo_cols:
                conn.execute(f"ALTER TABLE live_orders ADD COLUMN {col} {typedef}")
                print(f"[DB] Added {col} to live_orders")

    conn.commit()

def db_init():
    conn = db_connect()
    db_migrate(conn)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS system_state (
            id INTEGER PRIMARY KEY CHECK(id=1),
            onboarding_complete INTEGER NOT NULL DEFAULT 0,
            global_live_enabled INTEGER NOT NULL DEFAULT 0,
            initialized_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin TEXT NOT NULL,
            price REAL NOT NULL,
            ts INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_price_coin_ts ON price_history(coin, ts);

        CREATE TABLE IF NOT EXISTS kalshi_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin TEXT NOT NULL,
            window_ts INTEGER NOT NULL,
            market_ticker TEXT,
            yes_bid REAL,
            yes_ask REAL,
            last_price REAL,
            secs_left REAL,
            coin_price REAL,
            ts INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_kticks_coin_wts ON kalshi_ticks(coin, window_ts);

        CREATE TABLE IF NOT EXISTS polymarket_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin TEXT,
            market_id TEXT,
            question TEXT,
            yes_price REAL,
            volume REAL,
            ts INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_polyticks_ts ON polymarket_ticks(ts);

        CREATE TABLE IF NOT EXISTS paper_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            coin TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            starting_capital REAL NOT NULL DEFAULT 500.0,
            current_capital REAL NOT NULL DEFAULT 500.0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            total_pnl REAL NOT NULL DEFAULT 0.0,
            config_snapshot TEXT,
            started_at TEXT,
            ended_at TEXT,
            reset_reason TEXT,
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pruns_coin_status ON paper_runs(coin, status);

        CREATE TABLE IF NOT EXISTS paper_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin TEXT UNIQUE NOT NULL,
            run_id INTEGER,
            capital REAL NOT NULL DEFAULT 500.0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            total_pnl REAL NOT NULL DEFAULT 0.0,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin TEXT NOT NULL,
            run_id INTEGER,
            window_ts INTEGER NOT NULL,
            direction TEXT NOT NULL,
            actual TEXT,
            entry REAL NOT NULL,
            size REAL NOT NULL,
            contracts REAL NOT NULL,
            pnl REAL,
            fee REAL,
            result TEXT,
            balance REAL,
            coin_open REAL,
            coin_close REAL,
            decided_at TEXT,
            resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ptrades_coin ON paper_trades(coin, window_ts);

        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin TEXT NOT NULL,
            window_ts INTEGER NOT NULL,
            direction TEXT NOT NULL,
            entry REAL NOT NULL,
            confidence REAL,
            rationale TEXT,
            decided_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_uniq ON decisions(coin, window_ts);

        CREATE TABLE IF NOT EXISTS risk_settings (
            id INTEGER PRIMARY KEY CHECK(id=1),
            kill_switch INTEGER NOT NULL DEFAULT 0,
            daily_loss_limit REAL NOT NULL DEFAULT 100.0,
            max_drawdown_pct REAL NOT NULL DEFAULT 0.30,
            max_stake REAL NOT NULL DEFAULT 30.0,
            cooldown_after_losses INTEGER NOT NULL DEFAULT 3,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL DEFAULT 'system',
            event_type TEXT NOT NULL,
            object_type TEXT,
            object_id TEXT,
            payload TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(created_at);

        CREATE TABLE IF NOT EXISTS chat_messages_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            message TEXT NOT NULL,
            context_type TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages_v2(session_id);

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id INTEGER PRIMARY KEY,
            tooltips_enabled INTEGER NOT NULL DEFAULT 1,
            refresh_interval INTEGER NOT NULL DEFAULT 60,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS fill_quality (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin TEXT NOT NULL,
            window_ts INTEGER NOT NULL,
            ticker TEXT,
            direction TEXT,
            entry REAL,
            requested_contracts REAL,
            available_contracts REAL,
            filled_contracts REAL,
            liquidity_ok INTEGER,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS live_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin TEXT NOT NULL,
            window_ts INTEGER NOT NULL,
            ticker TEXT,
            direction TEXT,
            contracts INTEGER,
            limit_price INTEGER,
            order_id TEXT,
            status TEXT DEFAULT 'placed',
            error TEXT,
            filled_contracts INTEGER,
            avg_fill_price REAL,
            pnl REAL,
            actual_direction TEXT,
            resolved_at TEXT,
            created_at TEXT
        );
    """)

    # Seed system_state row
    conn.execute("""
        INSERT OR IGNORE INTO system_state (id, initialized_at, updated_at)
        VALUES (1, ?, ?)
    """, (now_cst().isoformat(), now_cst().isoformat()))

    # Seed risk_settings row
    conn.execute("""
        INSERT OR IGNORE INTO risk_settings (id, updated_at) VALUES (1, ?)
    """, (now_cst().isoformat(),))

    # Seed default filter settings (won't overwrite existing values)
    for k, v in [("min_confidence", "0.55"), ("min_volume", "500"),
                 ("exit_take_profit_pct", "40"), ("exit_stop_loss_pct", "65"),
                 ("exit_time_cliff_secs", "90"), ("exit_llm_check", "1"),
                 ("pool_multi_threshold", "0"),
                 ("blackout_hours", "8,10,11,17,18,23"),
                 ("autopause_wr_threshold", "0.42"),
                 ("poly_tracked_wallets", "")]:
        conn.execute("INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?,?,?)",
                     (k, v, now_cst().isoformat()))

    # Ensure paper accounts exist for all coins
    for coin in COINS:
        conn.execute(
            "INSERT OR IGNORE INTO paper_accounts (coin, capital, updated_at) VALUES (?, ?, ?)",
            (coin, STARTING_CAPITAL, now_cst().isoformat())
        )

    # Migrate live_orders to correct schema if it has wrong columns (Clyde's version)
    lo_cols = [r[1] for r in conn.execute("PRAGMA table_info(live_orders)").fetchall()]
    if "window_ts" not in lo_cols:
        conn.execute("DROP TABLE IF EXISTS live_orders")
        conn.execute("""
            CREATE TABLE live_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin TEXT NOT NULL,
                window_ts INTEGER NOT NULL,
                ticker TEXT,
                direction TEXT,
                contracts INTEGER,
                limit_price INTEGER,
                order_id TEXT,
                status TEXT DEFAULT 'placed',
                error TEXT,
                filled_contracts INTEGER,
                avg_fill_price REAL,
                created_at TEXT
            )
        """)

    conn.commit()
    conn.close()
    print("[DB] Initialized")

# ── Auth system ─────────────────────────────────────────────────────────────────
def hash_password(password):
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{h}"

def verify_password(password, stored):
    try:
        salt, h = stored.split(":", 1)
        return hashlib.sha256((salt + password).encode()).hexdigest() == h
    except:
        return False

def make_session_token(user_id, username):
    payload = f"{user_id}:{username}:{int(time.time())}"
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}|{sig}"
    return base64.b64encode(raw.encode()).decode()

def verify_session_token(token):
    try:
        raw = base64.b64decode(token.encode()).decode()
        payload, sig = raw.rsplit("|", 1)
        expected = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        parts = payload.split(":")
        ts = int(parts[2])
        if time.time() - ts > 86400 * 7:  # 7-day session
            return None
        return {"user_id": int(parts[0]), "username": parts[1]}
    except:
        return None

def get_session(handler):
    cookie_hdr = handler.headers.get("Cookie", "")
    for part in cookie_hdr.split(";"):
        part = part.strip()
        if part.startswith("autobet_session="):
            token = part[len("autobet_session="):]
            return verify_session_token(token)
    return None

def is_onboarding_complete():
    try:
        conn = db_connect()
        row = conn.execute("SELECT onboarding_complete FROM system_state WHERE id=1").fetchone()
        conn.close()
        return row and row[0] == 1
    except:
        return False

def get_user_count():
    try:
        conn = db_connect()
        n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        conn.close()
        return n
    except:
        return 0

def audit(event_type, object_type="", object_id="", payload=None, actor="system"):
    try:
        conn = db_connect()
        conn.execute(
            "INSERT INTO audit_logs (actor, event_type, object_type, object_id, payload, created_at) VALUES (?,?,?,?,?,?)",
            (actor, event_type, object_type, str(object_id),
             json.dumps(payload) if payload else None, now_cst().isoformat())
        )

        conn.commit()
        conn.close()
    except:
        pass

# ── In-memory state ─────────────────────────────────────────────────────────────
_state_lock    = threading.Lock()
_prices        = {}
_active_mkts   = {}
_last_collect  = {}
_poly_mkts     = {}   # coin -> latest polymarket data
_poly_wallets  = {}   # wallet_addr -> {coin, direction, size, ts} — copy-trading signals
_kalshi_comments = {}  # coin -> list of recent comment strings
_health_status = {}   # key -> {ok, msg, ts}

# ── Price collection ────────────────────────────────────────────────────────────
def fetch_coinbase_price(coin):
    if coin in COIN_PRICE_OVERRIDE:
        try:
            url = COIN_PRICE_OVERRIDE[coin]
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read().decode())
            # CoinGecko format: {"hyperliquid": {"usd": 12.34}}
            return float(list(data.values())[0]["usd"])
        except:
            return None
    url = COINBASE_URL.format(coin)
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return float(json.loads(r.read().decode())["data"]["amount"])
    except:
        return None

def collect_prices():
    while True:
        try:
            prices = {}
            for coin in COINS:
                p = fetch_coinbase_price(coin)
                if p:
                    prices[coin] = p
            with _state_lock:
                _prices.update(prices)
                _health_status["coinbase"] = {"ok": bool(prices), "msg": f"{len(prices)} coins", "ts": int(time.time())}
            ts = int(time.time())
            conn = db_connect()
            for coin, price in prices.items():
                conn.execute("INSERT INTO price_history (coin, price, ts) VALUES (?, ?, ?)", (coin, price, ts))

            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[PRICES] Error: {e}")
            with _state_lock:
                _health_status["coinbase"] = {"ok": False, "msg": str(e), "ts": int(time.time())}
        time.sleep(30)

# ── Kalshi market collection ────────────────────────────────────────────────────
def find_active_market(coin):
    series = SERIES.get(coin, f"KX{coin}15M")
    data = kalshi_get("/markets", {"series_ticker": series, "status": "open", "limit": 5})
    if not data:
        return None
    mkts = data.get("markets", [])
    if not mkts:
        return None
    m = mkts[0]
    def _price(key_dollars, key_cents=None):
        v = m.get(key_dollars)
        if v is not None:
            return float(v)
        if key_cents:
            v2 = m.get(key_cents)
            if v2 is not None:
                return float(v2) / 100.0
        return 0.0
    return {
        "ticker": m.get("ticker", ""),
        "title": m.get("title", ""),
        "yes_bid": _price("yes_bid_dollars", "yes_bid"),
        "yes_ask": _price("yes_ask_dollars", "yes_ask"),
        "last_price": _price("last_price_dollars", "last_price"),
        "volume": m.get("volume_fp") or m.get("volume", 0),
    }

def kalshi_window_ts():
    return (int(time.time()) // 900) * 900

def collect_kalshi():
    while True:
        try:
            wts = kalshi_window_ts()
            ts  = int(time.time())
            secs_left = wts + 900 - ts
            conn = db_connect()
            ok_count = 0
            for coin in COINS:
                try:
                    m = find_active_market(coin)
                    if not m:
                        continue
                    with _state_lock:
                        _active_mkts[coin] = {
                            "ticker": m["ticker"],
                            "window_ts": wts,
                            "yes_bid": m["yes_bid"],
                            "yes_ask": m["yes_ask"],
                            "last_price": m["last_price"],
                            "secs_left": secs_left,
                            "coin_price": _prices.get(coin),
                        }
                        _last_collect[coin] = ts
                    conn.execute("""
                        INSERT INTO kalshi_ticks
                        (coin, window_ts, market_ticker, yes_bid, yes_ask, last_price, secs_left, coin_price, ts)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (coin, wts, m["ticker"], m["yes_bid"], m["yes_ask"],
                          m["last_price"], secs_left, _prices.get(coin), ts))
                    ok_count += 1
                    # Fetch comments every ~5 minutes (every 5th collect cycle)
                    if ts % 300 < 65:
                        fetch_kalshi_comments(m["ticker"], coin)
                    time.sleep(0.5)
                except Exception as e:
                    print(f"[KALSHI] {coin}: {e}")

            conn.commit()
            conn.close()
            with _state_lock:
                _health_status["kalshi"] = {"ok": ok_count > 0, "msg": f"{ok_count}/{len(COINS)} coins", "ts": ts}
        except Exception as e:
            print(f"[KALSHI] Collection error: {e}")
            with _state_lock:
                _health_status["kalshi"] = {"ok": False, "msg": str(e), "ts": int(time.time())}
        time.sleep(60)

# ── Polymarket copy-trading wallet discovery ────────────────────────────────────
POLY_COPY_SEED = [
    # Direct wallet addresses from leaderboard (top 20 crypto/weekly/profit)
    "0xdE17f7144fbD0eddb2679132C10ff5e74B120988",  # #1  +$723k
    "0xB27BC932bf8110D8F78e55da7d5f0497A18B5b82",  # #5  +$412k
    "0x1f0ebc543B2d411f66947041625c0Aa1ce61CF86",  # #7  +$379k
    "0xd1ebE815f921b3EbBD8d9e0a4192C6Ab18360F5c",  # #12 +$225k
    # Resolved via proxyWallet from profile pages (@username → 0x...)
    "0xd84c2b6d65dc596f49c7b6aadd6d74ca91e407b9",  # BoneReader    #2  +$604k
    "0xd0d6053c3c37e727402d84c14069780d360993aa",  # k9Q2mX4L8A7ZP3R #3 +$506k
    "0x2d8b401d2f0e6937afebf18e19e11ca568a5260a",  # vidarx         #6  +$398k
    "0x0006af12cd4dacc450836a0e1ec6ce47365d8c63",  # stingo43       #8  +$323k
    "0xeebde7a0e019a63e6b476eb425505b7b3e6eba30",  # Bonereaper     #10 +$309k
    "0x29bc82f761749e67fa00d62896bc6855097b683c",  # BoshBashBish   #13 +$198k
    "0x70ec235a31eb35f243e2618d6ea3b5b8962bbb5d",  # vague-sourdough #14 +$190k
    "0xa45fe11dd1420fca906ceac2c067844379a42429",  # guh123         #15 +$187k
    "0x89b5cdaaa4866c1e738406712012a630b4078beb",  # ohanism        #19 +$164k
    "0xe9c6312464b52aa3eff13d822b003282075995c9",  # kingofcoinflips #20 +$163k
    "0x63ce342161250d705dc0b16df89036c8e5f9ba9a",  # 0x8dxd
    "0x732f189193d7a8c8bc8d8eb91f501a22736af081",  # 0x732F1
]
POLY_CRYPTO_KWS = ["bitcoin","btc","ethereum","eth","solana","sol","xrp","ripple","doge","bnb","hype"]
_last_wallet_discover = 0   # epoch timestamp of last leaderboard scrape

def _resolve_username_wallet(username):
    """Fetch a Polymarket profile page and extract the proxyWallet address."""
    import re as _re3
    try:
        req = urllib.request.Request(
            f"https://polymarket.com/@{username}",
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36"}
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            html = r.read().decode("utf-8", errors="ignore")
        wallets = _re3.findall(r'"proxyWallet"\s*:\s*"(0x[0-9a-fA-F]{40})"', html)
        return wallets[0] if wallets else None
    except Exception:
        return None

def _discover_poly_wallets():
    """Scrape Polymarket crypto leaderboard (addresses + usernames), resolve all to wallets."""
    global _last_wallet_discover
    import re as _re2
    try:
        req = urllib.request.Request(
            "https://polymarket.com/leaderboard/crypto/weekly/profit",
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
                     "Accept": "text/html,application/xhtml+xml"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="ignore")

        # Extract raw 0x addresses directly embedded in the page
        addr_found = list(dict.fromkeys(
            a for a in _re2.findall(r'0x[0-9a-fA-F]{40}\b', html)
            if len(a) == 42
        ))

        # Extract usernames (non-address display names in leaderboard entries)
        # Leaderboard names appear as /@username links
        usernames = list(dict.fromkeys(_re2.findall(r'/@([A-Za-z0-9_\-\.]{3,30})"', html)))
        # Filter out ones that look like raw addresses (they start with 0x)
        usernames = [u for u in usernames if not u.startswith("0x")]
        print(f"[COPY] Leaderboard scrape: {len(addr_found)} addresses, {len(usernames)} usernames")

        # Resolve usernames to proxyWallet addresses
        resolved = []
        for uname in usernames[:20]:
            addr = _resolve_username_wallet(uname)
            if addr:
                resolved.append(addr)
                print(f"[COPY] Resolved @{uname} → {addr}")
            time.sleep(0.3)

        all_candidates = list(dict.fromkeys(
            a.lower() for a in addr_found + resolved if len(a) == 42
        ))

        # Test each — keep only those with recent crypto activity
        good = []
        for addr in all_candidates[:50]:
            try:
                url = f"https://data-api.polymarket.com/activity?user={addr}&limit=20"
                req2 = urllib.request.Request(url, headers={"User-Agent": "autobet/1.0"})
                with urllib.request.urlopen(req2, timeout=8) as r2:
                    acts = json.loads(r2.read().decode())
                crypto = [a for a in acts if any(
                    kw in (a.get("title") or "").lower() for kw in POLY_CRYPTO_KWS
                )]
                if len(crypto) >= 3:
                    good.append(addr)
            except Exception:
                pass
            time.sleep(0.3)

        # Merge with seeds, deduplicate
        seen_lower = {}
        for a in POLY_COPY_SEED + good:
            seen_lower[a.lower()] = a
        merged = list(seen_lower.values())[:35]  # cap at 35 wallets

        conn_w = db_connect()
        conn_w.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?,?,?)",
                       ("poly_tracked_wallets", ",".join(merged), now_cst().isoformat()))
        conn_w.commit()
        conn_w.close()
        _last_wallet_discover = int(time.time())
        print(f"[COPY] Discovery complete: {len(resolved)} usernames resolved, {len(good)} active crypto wallets, {len(merged)} total tracked")
        return merged
    except Exception as e:
        print(f"[COPY] Discovery failed: {e}")
        return POLY_COPY_SEED

def _poll_copy_wallets(ts):
    """Poll tracked wallets for recent crypto trades; aggregate into coin-level signals."""
    global _last_wallet_discover
    # Auto-discover every 24 hours
    if ts - _last_wallet_discover > 86400:
        _discover_poly_wallets()

    conn_w = db_connect()
    wallet_row = conn_w.execute("SELECT value FROM settings WHERE key='poly_tracked_wallets'").fetchone()
    conn_w.close()
    if wallet_row and wallet_row[0].strip():
        wallets = [w.strip() for w in wallet_row[0].split(",") if w.strip()]
    else:
        wallets = POLY_COPY_SEED

    wallet_signals = {}
    for wallet in wallets:
        try:
            w_url = f"https://data-api.polymarket.com/activity?user={wallet}&limit=15"
            w_req = urllib.request.Request(w_url, headers={"User-Agent": "autobet/1.0"})
            with urllib.request.urlopen(w_req, timeout=10) as wr:
                activities = json.loads(wr.read().decode())
            if not isinstance(activities, list):
                activities = activities.get("data", [])
            for act in activities:
                act_ts = int(act.get("timestamp", act.get("createdAt", 0)) or 0)
                if ts - act_ts > 1200:  # only last 20 minutes
                    continue
                question = (act.get("title") or "").lower()
                outcome  = (act.get("outcome") or act.get("side") or "").strip().upper()
                size     = float(act.get("usdcSize") or act.get("size") or 0)
                for coin, kws in POLY_COIN_KEYWORDS.items():
                    if any(kw in question for kw in kws):
                        if coin not in wallet_signals:
                            wallet_signals[coin] = {"YES": 0, "NO": 0, "vol": 0.0, "wallets": []}
                        if outcome in ("YES", "BUY"):
                            wallet_signals[coin]["YES"] += 1
                            wallet_signals[coin]["vol"] += size
                        elif outcome in ("NO", "SELL"):
                            wallet_signals[coin]["NO"] += 1
                            wallet_signals[coin]["vol"] += size
                        label = wallet[:8] + "…"
                        if label not in wallet_signals[coin]["wallets"]:
                            wallet_signals[coin]["wallets"].append(label)
                        break
        except Exception:
            pass
        time.sleep(0.2)

    with _state_lock:
        _poly_wallets.clear()
        _poly_wallets.update(wallet_signals)
    if wallet_signals:
        sig_summary = {c: f"{v['YES']}Y/{v['NO']}N ${v['vol']:.0f}" for c, v in wallet_signals.items()}
        print(f"[COPY] Signals: {sig_summary}")

# ── Polymarket collection ───────────────────────────────────────────────────────
POLY_COIN_KEYWORDS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
    "SOL": ["solana", "sol"],
    "XRP": ["xrp", "ripple"],
}

def collect_polymarket():
    while True:
        try:
            url = f"{POLYMARKET_BASE}/markets?tag=crypto&limit=50&active=true"
            req = urllib.request.Request(url, headers={"User-Agent": "autobet/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
            markets = data if isinstance(data, list) else data.get("data", data.get("markets", []))
            ts = int(time.time())
            conn = db_connect()
            poly_update = {}
            for m in markets:
                question = (m.get("question") or m.get("title") or "").lower()
                mid = m.get("id") or m.get("market_id") or m.get("condition_id", "")
                # Try to find yes price from outcomes or tokens
                yes_price = None
                tokens = m.get("tokens") or m.get("outcomePrices") or []
                if isinstance(tokens, list) and tokens:
                    for tok in tokens:
                        if isinstance(tok, dict):
                            outcome = (tok.get("outcome") or tok.get("name") or "").lower()
                            if "yes" in outcome:
                                yes_price = float(tok.get("price") or tok.get("last_trade_price") or 0)
                                break
                if yes_price is None and isinstance(tokens, list) and len(tokens) >= 1:
                    try:
                        yes_price = float(tokens[0])
                    except:
                        pass
                volume = float(m.get("volume") or m.get("volume_num") or 0)
                # Map to coin
                for coin, kws in POLY_COIN_KEYWORDS.items():
                    if any(kw in question for kw in kws):
                        poly_update[coin] = {
                            "market_id": str(mid),
                            "question": m.get("question") or m.get("title", ""),
                            "yes_price": yes_price,
                            "volume": volume,
                            "ts": ts,
                        }
                        if yes_price is not None:
                            conn.execute("""
                                INSERT INTO polymarket_ticks (coin, market_id, question, yes_price, volume, ts)
                                VALUES (?,?,?,?,?,?)
                            """, (coin, str(mid), m.get("question") or m.get("title",""), yes_price, volume, ts))
                        break

            conn.commit()
            conn.close()
            with _state_lock:
                _poly_mkts.update(poly_update)
                _health_status["polymarket"] = {"ok": bool(poly_update), "msg": f"{len(poly_update)} coins mapped", "ts": ts}

            # Feature 7: Copy-trading — auto-discover + poll top wallets
            try:
                _poll_copy_wallets(ts)
            except Exception as ce:
                print(f"[COPY] {ce}")

        except Exception as e:
            print(f"[POLYMARKET] Error: {e}")
            with _state_lock:
                _health_status["polymarket"] = {"ok": False, "msg": str(e), "ts": int(time.time())}
        time.sleep(90)

# ── Risk engine ─────────────────────────────────────────────────────────────────
def get_risk_settings():
    try:
        conn = db_connect()
        row = conn.execute("SELECT kill_switch, daily_loss_limit, max_drawdown_pct, max_stake, cooldown_after_losses FROM risk_settings WHERE id=1").fetchone()
        conn.close()
        if row:
            return {"kill_switch": bool(row[0]), "daily_loss_limit": row[1],
                    "max_drawdown_pct": row[2], "max_stake": row[3], "cooldown_after_losses": row[4]}
    except:
        pass
    return {"kill_switch": False, "daily_loss_limit": 100.0, "max_drawdown_pct": 0.30, "max_stake": 30.0, "cooldown_after_losses": 3}

def check_risk(coin, direction, entry, size):
    """Returns (ok: bool, reason: str). Called before placing any paper or live trade."""
    rs = get_risk_settings()
    if rs["kill_switch"]:
        return False, "Kill switch is active — all trading halted"
    if size > rs["max_stake"]:
        return False, f"Size ${size:.2f} exceeds max stake ${rs['max_stake']:.2f}"
    if entry < ENTRY_FLOOR:
        return False, f"Entry {entry:.4f} below floor {ENTRY_FLOOR} — unrealistic liquidity (lottery ticket)"
    if entry > ENTRY_CEILING:
        return False, f"Entry {entry:.4f} above ceiling {ENTRY_CEILING} — confirmed negative EV across all coins"
    # Get run-reset timestamp — guardrail counters only look at trades AFTER last reset
    conn = db_connect()
    reset_row = conn.execute("SELECT value FROM settings WHERE key=?", (f"cooldown_reset_{coin}",)).fetchone()
    conn.close()
    reset_after = reset_row[0] if reset_row else "2000-01-01"

    # Daily loss check (only trades since today AND since last run reset)
    try:
        today_str = now_cst().strftime("%Y-%m-%d")
        conn = db_connect()
        row = conn.execute("""
            SELECT COALESCE(SUM(pnl),0) FROM paper_trades
            WHERE coin=? AND result='LOSS'
              AND resolved_at >= ? AND resolved_at >= ?
        """, (coin, today_str, reset_after)).fetchone()
        daily_loss = abs(float(row[0])) if row else 0.0
        conn.close()
        if daily_loss >= rs["daily_loss_limit"]:
            return False, f"Daily loss limit ${rs['daily_loss_limit']:.0f} reached for {coin} (${daily_loss:.2f} lost today)"
    except:
        pass
    # Drawdown check — uses the active run's starting capital, not global constant
    try:
        conn = db_connect()
        acct = conn.execute("SELECT capital FROM paper_accounts WHERE coin=?", (coin,)).fetchone()
        run_row = conn.execute(
            "SELECT starting_capital FROM paper_runs WHERE coin=? AND status='active' ORDER BY id DESC LIMIT 1", (coin,)
        ).fetchone()
        conn.close()
        if acct:
            capital = float(acct[0])
            start_cap = float(run_row[0]) if run_row and run_row[0] else capital
            floor = start_cap * (1.0 - rs["max_drawdown_pct"])
            if capital < floor:
                return False, f"{coin} capital ${capital:.2f} below max drawdown floor ${floor:.2f} ({rs['max_drawdown_pct']*100:.0f}% of ${start_cap:.0f} starting)"
    except:
        pass
    # Cooldown check (only consecutive losses after last run reset)
    try:
        conn = db_connect()
        recent = conn.execute("""
            SELECT result FROM paper_trades WHERE coin=? AND result IS NOT NULL
              AND (resolved_at IS NULL OR resolved_at >= ?)
            ORDER BY window_ts DESC LIMIT ?
        """, (coin, reset_after, rs["cooldown_after_losses"])).fetchall()
        conn.close()
        if len(recent) >= rs["cooldown_after_losses"] and all(r[0] == "LOSS" for r in recent):
            return False, f"{coin} in cooldown: {rs['cooldown_after_losses']} consecutive losses"
    except:
        pass
    return True, "ok"

# ── Paper runs ──────────────────────────────────────────────────────────────────
def get_active_run_id(coin, conn=None):
    """Get the active paper_run id for a coin, creating one if needed.
    Accepts an existing connection to avoid opening a second one mid-transaction."""
    own_conn = conn is None
    if own_conn:
        conn = db_connect()
    row = conn.execute(
        "SELECT id FROM paper_runs WHERE coin=? AND status='active' ORDER BY id DESC LIMIT 1",
        (coin,)
    ).fetchone()
    if row:
        if own_conn:
            conn.close()
        return row[0]
    # Create initial run
    rs = get_risk_settings()
    snap = json.dumps({"model": get_minimax_model(), "trade_size": TRADE_SIZE, "started": now_cst().isoformat()})
    conn.execute("""
        INSERT INTO paper_runs (name, coin, status, starting_capital, current_capital, config_snapshot, started_at, created_at)
        VALUES (?, ?, 'active', ?, ?, ?, ?, ?)
    """, (f"{coin} Run 1", coin, STARTING_CAPITAL, STARTING_CAPITAL, snap, now_cst().isoformat(), now_cst().isoformat()))
    if own_conn:
        conn.commit()
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    if own_conn:
        conn.close()
    return run_id

def archive_run(coin, reason="manual"):
    """Archive the active run for a coin and create a new one."""
    conn = db_connect()
    row = conn.execute(
        "SELECT id, name FROM paper_runs WHERE coin=? AND status='active' ORDER BY id DESC LIMIT 1",
        (coin,)
    ).fetchone()
    if row:
        run_id, old_name = row
        conn.execute("""
            UPDATE paper_runs SET status='archived', ended_at=?, reset_reason=? WHERE id=?
        """, (now_cst().isoformat(), reason, run_id))
    # Count existing runs for this coin
    count = conn.execute("SELECT COUNT(*) FROM paper_runs WHERE coin=?", (coin,)).fetchone()[0]
    snap = json.dumps({"model": get_minimax_model(), "trade_size": TRADE_SIZE, "started": now_cst().isoformat()})
    cap_row = conn.execute("SELECT value FROM settings WHERE key='starting_capital'").fetchone()
    starting = float(cap_row[0]) if cap_row else STARTING_CAPITAL
    conn.execute("""
        INSERT INTO paper_runs (name, coin, status, starting_capital, current_capital, config_snapshot, started_at, created_at)
        VALUES (?, ?, 'active', ?, ?, ?, ?, ?)
    """, (f"{coin} Run {count + 1}", coin, starting, starting, snap, now_cst().isoformat(), now_cst().isoformat()))

    conn.commit()
    conn.close()
    # Reset paper account
    conn2 = db_connect()
    conn2.execute("UPDATE paper_accounts SET capital=?, wins=0, losses=0, total_pnl=0, updated_at=? WHERE coin=?",
                  (starting, now_cst().isoformat(), coin))
    # Reset cooldown state stored in settings so daily-loss and losing-streak
    # guardrails start fresh. The kill_switch and limits themselves are NOT touched —
    # only the counters that accumulate against those limits.
    conn2.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?,?,?)",
                  (f"cooldown_reset_{coin}", now_cst().isoformat(), now_cst().isoformat()))
    conn2.commit()
    conn2.close()
    audit("paper_run_archived", "paper_run", coin, {"reason": reason})

# ── Decision loop ───────────────────────────────────────────────────────────────
def get_recent_ticks(conn, coin, wts, n=8):
    return conn.execute("""
        SELECT window_ts, yes_bid, yes_ask, coin_price, secs_left
        FROM kalshi_ticks WHERE coin=? AND window_ts=?
        ORDER BY ts DESC LIMIT ?
    """, (coin, wts, n)).fetchall()

def get_price_at(conn, coin, ts, tol=120):
    row = conn.execute("""
        SELECT price FROM price_history WHERE coin=? AND ABS(ts - ?) <= ?
        ORDER BY ABS(ts - ?) LIMIT 1
    """, (coin, ts, tol, ts)).fetchone()
    return row[0] if row else None

def minimax_analyze(coin, ticks_summary, coin_price, market_volume=None, spread=None,
                    rules_signal=None, knn_signal=None, price_momentum=None,
                    poly_price=None, prev_outcomes=None, fee_note=None):
    if not MINIMAX_KEY:
        return None
    vol_line = ""
    if market_volume is not None:
        vol_activity = "HIGH" if market_volume >= 2000 else ("MODERATE" if market_volume >= 500 else "LOW")
        vol_line = f"\n24h market volume: {int(market_volume):,} contracts ({vol_activity} activity)"
    spread_line = ""
    if spread is not None:
        spread_line = f"\nCurrent bid-ask spread: {spread:.3f} ({'tight' if spread < 0.05 else 'wide — slippage risk'})"

    # Build engine results block
    engine_lines = []
    agreement_count = {"YES": 0, "NO": 0}

    if rules_signal:
        rd = rules_signal.get("direction", "PASS")
        re_ = rules_signal.get("entry", 0)
        rc = rules_signal.get("confidence", 0)
        mid = re_ if rd == "YES" else round(1 - re_, 3)
        zone = "above 0.62 YES threshold" if mid > 0.62 else ("below 0.38 NO threshold" if mid < 0.38 else "neutral zone")
        engine_lines.append(f"- Rules engine:    {rd:4s}  entry={re_:.3f}  conf={rc:.2f}  (market mid={mid:.3f}, {zone})")
        if rd in ("YES", "NO"):
            agreement_count[rd] += 1
    else:
        engine_lines.append(f"- Rules engine:    PASS  (market mid in neutral zone 0.38–0.62, no strong order book signal)")

    if knn_signal:
        kd = knn_signal.get("direction", "PASS")
        ke = knn_signal.get("entry", 0)
        kc = knn_signal.get("confidence", 0)
        kr = knn_signal.get("rationale", "")
        engine_lines.append(f"- KNN (historical): {kd:4s}  entry={ke:.3f}  conf={kc:.2f}  ({kr})")
        if kd in ("YES", "NO"):
            agreement_count[kd] += 1
    else:
        engine_lines.append(f"- KNN (historical): insufficient resolved history for this coin")

    if price_momentum is not None:
        pct = price_momentum * 100
        trend = "bullish" if pct > 0.1 else ("bearish" if pct < -0.1 else "flat")
        engine_lines.append(f"- Price momentum:  {'+' if pct>=0 else ''}{pct:.2f}% over last 5 min ({trend})")
        if pct > 0.15:
            agreement_count["YES"] += 1
        elif pct < -0.15:
            agreement_count["NO"] += 1

    # Consensus summary
    yes_n, no_n = agreement_count["YES"], agreement_count["NO"]
    if yes_n >= 2 and no_n == 0:
        consensus = f"STRONG CONSENSUS: all {yes_n} signals point YES — override requires explicit reasoning"
    elif no_n >= 2 and yes_n == 0:
        consensus = f"STRONG CONSENSUS: all {no_n} signals point NO — override requires explicit reasoning"
    elif yes_n > 0 and no_n > 0:
        consensus = f"MIXED SIGNALS: {yes_n} YES vs {no_n} NO — use your judgment, lower confidence is appropriate"
    else:
        consensus = "WEAK/NO CONSENSUS: limited quantitative signal, rely on order book analysis"

    # Polymarket cross-venue signal
    if poly_price is not None:
        try:
            rules_mid = None
            if rules_signal:
                re_ = rules_signal.get("entry", 0)
                rd_ = rules_signal.get("direction", "")
                rules_mid = re_ if rd_ == "YES" else round(1 - re_, 3) if re_ else None
            kalshi_mid_est = rules_mid or 0.5
            diff = poly_price - kalshi_mid_est
            arb_note = f"arb gap {abs(diff):.3f}" if abs(diff) > 0.05 else "aligned with Kalshi"
            engine_lines.append(f"- Polymarket:      YES={poly_price:.3f}  ({arb_note})")
            if poly_price > 0.62:
                agreement_count["YES"] += 1
            elif poly_price < 0.38:
                agreement_count["NO"] += 1
        except Exception:
            pass

    # Previous window outcomes (recent momentum)
    if prev_outcomes:
        engine_lines.append(f"- Recent outcomes: {prev_outcomes}  (window sequence, newest first)")

    # Fee-adjusted EV note
    fee_line = f"\n{fee_note}" if fee_note else ""

    engine_block = ("\n\nEngine results (pre-computed before your analysis):\n"
                    + "\n".join(engine_lines)
                    + f"\nConsensus: {consensus}"
                    + fee_line)

    prompt = f"""You are the final decision-maker in a multi-engine prediction market trading system for 15-minute {coin} price contracts.

Current {coin} spot price: ${coin_price:,.2f}{vol_line}{spread_line}

Order book snapshots from the last 5 minutes (yes_bid = probability market pays for UP outcome, yes_ask = cost to acquire UP position):
{ticks_summary}{engine_block}

Your task: Synthesize all available signals and make the final trading decision.
- If HIGHER: direction=YES, entry=yes_ask value from the order book above
- If LOWER: direction=NO, entry=(1 - yes_bid) value from the order book above
- Strong consensus from multiple engines = high confidence. Mixed signals = lower confidence or PASS.
- Wide spreads and low volume increase risk — reduce confidence accordingly.
- Before finalizing: consider the strongest argument AGAINST your chosen direction. If that counter-argument is compelling, reduce your confidence below 0.50 — the system will automatically skip low-confidence trades. Always output YES or NO as the direction (never any other value); use confidence to express uncertainty.
- Only suggest an engine switch if you have strong conviction that a different engine would perform significantly better for THIS coin's current market regime (e.g. strong sustained trend → rules_engine, rich resolved history → vector_knn). Default to null — do NOT suggest a switch just because signals are mixed or uncertain. A suggestion triggers an auto-switch after several consecutive windows, so only suggest when you are confident it will improve results long-term.

Respond with JSON only:
{{"direction": "YES" or "NO", "entry": 0.XX, "confidence": 0.0-1.0, "rationale": "one line including what you considered and why you held or revised", "suggest_engine": null or "rules_engine" or "vector_knn" or "hybrid"}}"""

    # Run MiniMax and local 35B in parallel — use both results to validate
    import threading as _threading
    results = {}

    def _call_minimax():
        try:
            payload = json.dumps({
                "model": get_minimax_model(),
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}]
            }).encode()
            req = urllib.request.Request(
                MINIMAX_URL, data=payload,
                headers={"Content-Type": "application/json", "x-api-key": MINIMAX_KEY, "anthropic-version": "2023-06-01"}
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read().decode())
                text = ""
                for block in resp.get("content", []):
                    if block.get("type") == "text":
                        text = block.get("text", "").strip()
                        break
                if not text:
                    return
                if "```" in text:
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                text = text.strip()
                try:
                    results["minimax"] = json.loads(text)
                except Exception:
                    import re as _re2
                    dir_m  = _re2.search(r'"direction"\s*:\s*"(YES|NO)"', text)
                    ent_m  = _re2.search(r'"entry"\s*:\s*([0-9.]+)', text)
                    conf_m = _re2.search(r'"confidence"\s*:\s*([0-9.]+)', text)
                    rat_m  = _re2.search(r'"rationale"\s*:\s*"([^"]*)', text)
                    sug_m  = _re2.search(r'"suggest_engine"\s*:\s*"([^"]*)"', text)
                    if dir_m and ent_m:
                        results["minimax"] = {
                            "direction":      dir_m.group(1),
                            "entry":          float(ent_m.group(1)),
                            "confidence":     float(conf_m.group(1)) if conf_m else 0.5,
                            "rationale":      rat_m.group(1)[:80] if rat_m else "parsed from partial response",
                            "suggest_engine": sug_m.group(1) if sug_m else None,
                        }
        except Exception as e:
            print(f"[MINIMAX] {coin}: {e}")

    def _call_minimax2():
        """Second independent MiniMax call — adversarial framing, higher temperature."""
        try:
            adversarial_prompt = (
                prompt.replace(
                    "Synthesize all available signals and make the final trading decision.",
                    "You are a SKEPTIC. Your job is to challenge the consensus. Assume the obvious direction is wrong — what would make you go the OTHER way? Only go with the consensus if you cannot find a compelling counter-argument."
                )
            )
            payload = json.dumps({
                "model": get_minimax_model(),
                "max_tokens": 4096,
                "temperature": 0.4,
                "messages": [{"role": "user", "content": adversarial_prompt}]
            }).encode()
            req = urllib.request.Request(
                MINIMAX_URL, data=payload,
                headers={"Content-Type": "application/json", "x-api-key": MINIMAX_KEY, "anthropic-version": "2023-06-01"}
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read().decode())
                text = ""
                for block in resp.get("content", []):
                    if block.get("type") == "text":
                        text = block.get("text", "").strip()
                        break
                if not text:
                    return
                if "```" in text:
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                text = text.strip()
                try:
                    results["minimax2"] = json.loads(text)
                except Exception:
                    import re as _re2
                    dir_m  = _re2.search(r'"direction"\s*:\s*"(YES|NO)"', text)
                    ent_m  = _re2.search(r'"entry"\s*:\s*([0-9.]+)', text)
                    conf_m = _re2.search(r'"confidence"\s*:\s*([0-9.]+)', text)
                    rat_m  = _re2.search(r'"rationale"\s*:\s*"([^"]*)', text)
                    if dir_m and ent_m:
                        results["minimax2"] = {
                            "direction":  dir_m.group(1),
                            "entry":      float(ent_m.group(1)),
                            "confidence": float(conf_m.group(1)) if conf_m else 0.5,
                            "rationale":  rat_m.group(1)[:80] if rat_m else "parsed",
                            "suggest_engine": None,
                        }
        except Exception as e:
            print(f"[MINIMAX2] {coin}: {e}")

    t_mm  = _threading.Thread(target=_call_minimax,  daemon=True)
    t_loc = _threading.Thread(target=_call_minimax2, daemon=True)
    t_mm.start()
    t_loc.start()
    t_mm.join(timeout=125)
    t_loc.join(timeout=125)

    mm = results.get("minimax")
    lo = results.get("minimax2")

    # Treat PASS direction as no-signal (model expressing uncertainty via direction field)
    if mm and mm.get("direction") not in ("YES", "NO"):
        print(f"[DUAL LLM] {coin}: MiniMax#1 returned direction={mm.get('direction')} — treating as no signal")
        mm = None
    if lo and lo.get("direction") not in ("YES", "NO"):
        lo = None

    if mm and lo:
        mm_dir, lo_dir = mm.get("direction"), lo.get("direction")
        mm_conf, lo_conf = float(mm.get("confidence", 0.5)), float(lo.get("confidence", 0.5))
        if mm_dir == lo_dir:
            boosted = round(min((mm_conf + lo_conf) / 2 * 1.08, 0.95), 4)
            mm["confidence"] = boosted
            mm["rationale"]  = mm["rationale"].rstrip(".") + f" [skeptic agrees {lo_conf:.2f}↑]"
            print(f"[DUAL LLM] {coin}: AGREE {mm_dir} mm={mm_conf:.2f} skeptic={lo_conf:.2f} → {boosted:.2f}")
        else:
            penalized = round(mm_conf * 0.65, 4)
            mm["confidence"] = penalized
            mm["rationale"]  = mm["rationale"].rstrip(".") + f" [skeptic disagrees: {lo_dir} {lo_conf:.2f}↓]"
            print(f"[DUAL LLM] {coin}: DISAGREE mm={mm_dir}({mm_conf:.2f}) skeptic={lo_dir}({lo_conf:.2f}) → penalized to {penalized:.2f}")
        result = mm
    elif mm:
        print(f"[DUAL LLM] {coin}: primary only (skeptic timed out)")
        result = mm
    elif lo:
        print(f"[DUAL LLM] {coin}: skeptic only (primary failed)")
        lo["rationale"] = lo.get("rationale","").rstrip(".") + " [skeptic only]"
        result = lo
    else:
        return None

    sug = result.get("suggest_engine")
    if sug and sug in ("rules_engine", "vector_knn", "hybrid", "minimax_llm"):
        print(f"[ENGINE HINT] {coin}: LLM suggests switching to {sug} — {result.get('rationale','')[:60]}")
    return result

def format_ticks_summary(ticks):
    if not ticks:
        return "No tick data available."
    lines = []
    for t in ticks:
        wts, yes_bid, yes_ask, coin_price, secs_left = t
        mid = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else None
        mid_str = f"{mid:.3f}" if mid else "?"
        price_str = f"${coin_price:,.2f}" if coin_price else "?"
        lines.append(f"  secs_left={secs_left:.0f}  yes_bid={yes_bid:.3f}  yes_ask={yes_ask:.3f}  mid={mid_str}  price={price_str}")
    return "\n".join(lines)

def resolve_trades():
    now = int(time.time())
    conn = db_connect()
    trades = conn.execute("""
        SELECT id, coin, window_ts, direction, entry, size, contracts, coin_open
        FROM paper_trades WHERE result IS NULL AND ? > window_ts + 1020
    """, (now,)).fetchall()

    for t in trades:
        tid, coin, wts, direction, entry, size, contracts, coin_open = t
        coin_close = get_price_at(conn, coin, wts + 900, tol=600)
        if not coin_close or not coin_open:
            continue
        actual = "YES" if coin_close > coin_open else "NO"
        if direction == actual:
            gross = contracts * (1.0 - entry)
            # Correct Kalshi fee: 7% of entry*(1-entry) per contract, capped at $0.02/contract
            fee_per = min(KALSHI_FEE_RATE * entry * (1.0 - entry), 0.02)
            fee = contracts * fee_per
            pnl = gross - fee
            result = "WIN"
        else:
            pnl = -(contracts * entry)
            fee = 0.0
            result = "LOSS"
        acct = conn.execute("SELECT capital, wins, losses, total_pnl FROM paper_accounts WHERE coin=?", (coin,)).fetchone()
        if acct:
            new_cap = acct[0] + pnl
            conn.execute("""
                UPDATE paper_accounts SET capital=?, wins=?, losses=?, total_pnl=?, updated_at=? WHERE coin=?
            """, (round(new_cap, 4),
                  acct[1] + (1 if result == "WIN" else 0),
                  acct[2] + (0 if result == "WIN" else 1),
                  round(acct[3] + pnl, 4),
                  now_cst().isoformat(), coin))
            # Update active paper run
            try:
                run_id = get_active_run_id(coin, conn)
                conn.execute("""
                    UPDATE paper_runs SET current_capital=?,
                    wins = wins + ?, losses = losses + ?,
                    total_pnl = total_pnl + ?
                    WHERE id=?
                """, (round(new_cap, 4), 1 if result=="WIN" else 0, 0 if result=="WIN" else 1,
                      round(pnl, 4), run_id))
            except:
                pass
        conn.execute("""
            UPDATE paper_trades SET actual=?, pnl=?, fee=?, result=?, balance=?,
                coin_close=?, resolved_at=? WHERE id=?
        """, (actual, round(pnl, 4), round(fee, 4), result,
              round((acct[0] if acct else STARTING_CAPITAL) + pnl, 2),
              coin_close, now_cst().isoformat(), tid))

    conn.commit()
    conn.close()

def resolve_live_orders():
    """
    For filled live orders whose window has closed, fetch the Kalshi market
    settlement outcome and record actual P&L. Runs alongside resolve_trades().
    """
    now = int(time.time())
    conn = db_connect()
    orders = conn.execute("""
        SELECT id, coin, window_ts, ticker, direction, filled_contracts, avg_fill_price
        FROM live_orders
        WHERE status IN ('filled', 'filled_partial', 'executed')
          AND resolved_at IS NULL
          AND ? > window_ts + 1020
    """, (now,)).fetchall()
    conn.close()

    for row in orders:
        lo_id, coin, wts, ticker, direction, filled_count, avg_price = row
        if not ticker or not filled_count or not avg_price:
            continue
        filled_count = int(filled_count)
        avg_price = float(avg_price)

        # Fetch settled market from Kalshi to get actual outcome
        mkt_data = kalshi_get(f"/markets/{ticker}")
        if not mkt_data:
            continue
        mkt = mkt_data.get("market", mkt_data)
        kalshi_result = (mkt.get("result") or "").lower()  # "yes" or "no"
        if kalshi_result not in ("yes", "no"):
            # Market not settled yet — try again next cycle
            continue

        actual_direction = "YES" if kalshi_result == "yes" else "NO"
        won = (direction == actual_direction)

        if won:
            gross = filled_count * (1.0 - avg_price)
            fee_per = min(KALSHI_FEE_RATE * avg_price * (1.0 - avg_price), 0.02)
            fee = filled_count * fee_per
            pnl = round(gross - fee, 4)
        else:
            pnl = round(-(filled_count * avg_price), 4)

        conn2 = db_connect()
        conn2.execute("""
            UPDATE live_orders SET pnl=?, actual_direction=?, resolved_at=? WHERE id=?
        """, (pnl, actual_direction, now_cst().isoformat(), lo_id))
        conn2.commit()
        conn2.close()
        result_str = "WIN" if won else "LOSS"
        print(f"[LIVE RESOLVE] {coin} wts={wts} {direction} vs {actual_direction} -> {result_str}  pnl={pnl:+.4f}")
        audit("live_order_resolved", "live_order", str(lo_id),
              {"coin": coin, "wts": wts, "direction": direction,
               "actual": actual_direction, "result": result_str, "pnl": pnl})


def decision_loop():
    global _pool_last_placed_wts
    time.sleep(10)
    last_decided_wts = 0
    while True:
        try:
            wts = kalshi_window_ts()
            now = int(time.time())
            secs_into = now - wts
            if secs_into > 180:
                # Still run resolution even when outside decision window
                try:
                    resolve_trades()
                    resolve_live_orders()
                except Exception:
                    pass
                time.sleep(30)
                continue
            if wts <= last_decided_wts:
                time.sleep(30)
                continue
            def _decide_coin(coin, wts, now, pool_signals=None, pool_lock=None, stale_counter=None, api_delay=0):
              try:
                conn = db_connect()
                try:
                    existing = conn.execute(
                        "SELECT id FROM decisions WHERE coin=? AND window_ts=?", (coin, wts)
                    ).fetchone()
                    if existing:
                        return
                    with _state_lock:
                        mkt = _active_mkts.get(coin)
                        coin_price = _prices.get(coin)
                    if not mkt or mkt.get("window_ts") != wts:
                        mkt_wts = mkt.get("window_ts") if mkt else None
                        print(f"[SKIP] {coin} wts={wts}: stale market data (mkt_wts={mkt_wts})")
                        if stale_counter is not None:
                            stale_counter.append(1)
                        return
                    if not coin_price:
                        print(f"[SKIP] {coin} wts={wts}: no coin price")
                        return
                    # ── Feature 9: Window entry timing delay ────────────────
                    # Avoid thin early liquidity — wait until at least 60s into window
                    secs_into_window = int(time.time()) - wts
                    if secs_into_window < 60:
                        wait_secs = 60 - secs_into_window
                        print(f"[TIMING] {coin} wts={wts}: waiting {wait_secs}s (only {secs_into_window}s into window)")
                        time.sleep(wait_secs)

                    # Stagger MiniMax API calls only — market data already checked above
                    if api_delay:
                        time.sleep(api_delay)
                    prev_wts = wts - 900
                    ticks = get_recent_ticks(conn, coin, prev_wts, n=10)
                    ticks_summary = format_ticks_summary(ticks)

                    # ── Volume pre-trade filter ──────────────────────────────
                    market_volume = float(mkt.get("volume") or 0)
                    yes_bid_v = float(mkt.get("yes_bid") or 0)
                    yes_ask_v = float(mkt.get("yes_ask") or 0)
                    spread_v  = round(yes_ask_v - yes_bid_v, 4) if yes_bid_v and yes_ask_v else None
                    try:
                        min_vol = float(conn.execute(
                            "SELECT value FROM settings WHERE key='min_volume'"
                        ).fetchone()[0])
                    except Exception:
                        min_vol = 500.0
                    if min_vol > 0 and market_volume > 0 and market_volume < min_vol:
                        conn.execute("""
                            INSERT OR IGNORE INTO decisions (coin, window_ts, direction, entry, confidence, rationale, decided_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (coin, wts, "PASS", round(yes_ask_v, 4), 0.0,
                              f"Volume PASS: {int(market_volume)} contracts < {int(min_vol)} threshold",
                              now_cst().isoformat()))
                        conn.commit()
                        print(f"[VOLUME] {coin} wts={wts}: PASS — volume {int(market_volume)} < {int(min_vol)}")
                        return

                    # ── Feature 1: Time-of-day blackout filter ───────────────
                    try:
                        bh_row = conn.execute(
                            "SELECT value FROM settings WHERE key='blackout_hours'"
                        ).fetchone()
                        if bh_row and bh_row[0].strip():
                            blackout = [int(h.strip()) for h in bh_row[0].split(",") if h.strip().isdigit()]
                            cur_hour = now_cst().hour
                            if cur_hour in blackout:
                                conn.execute("""
                                    INSERT OR IGNORE INTO decisions (coin, window_ts, direction, entry, confidence, rationale, decided_at)
                                    VALUES (?,?,?,?,?,?,?)
                                """, (coin, wts, "PASS", round(yes_ask_v, 4), 0.0,
                                      f"Blackout PASS: hour {cur_hour}:00 CT in blackout list",
                                      now_cst().isoformat()))
                                conn.commit()
                                print(f"[BLACKOUT] {coin} wts={wts}: PASS — hour {cur_hour} blacklisted")
                                return
                    except Exception:
                        pass

                    # ── Feature 2b: Coin auto-pause by rolling WR ────────────
                    try:
                        ap_row = conn.execute(
                            "SELECT value FROM settings WHERE key='autopause_wr_threshold'"
                        ).fetchone()
                        autopause_threshold = float(ap_row[0]) if ap_row else 0.0
                        if autopause_threshold > 0:
                            ap_rows = conn.execute("""
                                SELECT result FROM paper_trades
                                WHERE coin=? AND result IN ('WIN','LOSS')
                                ORDER BY window_ts DESC LIMIT 15
                            """, (coin,)).fetchall()
                            if len(ap_rows) >= 10:
                                wr = sum(1 for r in ap_rows if r[0]=='WIN') / len(ap_rows)
                                if wr < autopause_threshold:
                                    conn.execute("""
                                        INSERT OR IGNORE INTO decisions (coin, window_ts, direction, entry, confidence, rationale, decided_at)
                                        VALUES (?,?,?,?,?,?,?)
                                    """, (coin, wts, "PASS", round(yes_ask_v, 4), 0.0,
                                          f"AutoPause: {coin} WR {wr:.0%} over last {len(ap_rows)} trades < {autopause_threshold:.0%} threshold",
                                          now_cst().isoformat()))
                                    conn.commit()
                                    print(f"[AUTOPAUSE] {coin} wts={wts}: PASS — WR {wr:.0%} below {autopause_threshold:.0%}")
                                    return
                    except Exception:
                        pass

                    # ── Feature 6: Block entry in last 2 minutes ─────────────
                    secs_left_now = max(0, wts + 900 - int(time.time()))
                    if secs_left_now < 120:
                        conn.execute("""
                            INSERT OR IGNORE INTO decisions (coin, window_ts, direction, entry, confidence, rationale, decided_at)
                            VALUES (?,?,?,?,?,?,?)
                        """, (coin, wts, "PASS", round(yes_ask_v, 4), 0.0,
                              f"Timing PASS: only {secs_left_now}s left in window (< 120s entry block)",
                              now_cst().isoformat()))
                        conn.commit()
                        print(f"[TIMING] {coin} wts={wts}: PASS — {secs_left_now}s left, too late to enter")
                        return

                    # ── Feature 2: Polymarket cross-venue signal ─────────────
                    with _state_lock:
                        poly_price = _poly_mkts.get(coin, {}).get("yes_price")
                        wallet_sig = dict(_poly_wallets.get(coin, {}))
                        comments   = list(_kalshi_comments.get(coin, []))

                    # ── Feature 3: Previous window outcomes ──────────────────
                    prev_outcomes = None
                    try:
                        prev_rows = conn.execute("""
                            SELECT result FROM paper_trades
                            WHERE coin=? AND result IN ('WIN','LOSS')
                            ORDER BY window_ts DESC LIMIT 3
                        """, (coin,)).fetchall()
                        if prev_rows:
                            prev_outcomes = "/".join(r[0] for r in prev_rows)
                    except Exception:
                        pass

                    # ── Feature 8: Fee-adjusted EV note ─────────────────────
                    fee_note = None
                    try:
                        yes_mid_est = (yes_bid_v + yes_ask_v) / 2 if yes_bid_v and yes_ask_v else None
                        if yes_mid_est:
                            fee_per_c = round(KALSHI_FEE_RATE * yes_mid_est * (1 - yes_mid_est), 4)
                            net_entry_yes = round(yes_ask_v + fee_per_c, 4)
                            net_entry_no  = round((1 - yes_bid_v) + fee_per_c, 4)
                            fee_note = (f"Fee-adjusted cost: YES entry {net_entry_yes:.3f} "
                                        f"(raw {yes_ask_v:.3f} + fee {fee_per_c:.4f}/contract), "
                                        f"NO entry {net_entry_no:.3f} — factor fees into EV")
                    except Exception:
                        pass

                    # ── Feature 6: Order book depth summary ─────────────────
                    ob_depth_note = None
                    try:
                        ticker_for_ob = mkt.get("ticker", "")
                        if ticker_for_ob:
                            ob_data = fetch_kalshi_order_book(ticker_for_ob)
                            if ob_data:
                                ob_depth_note = ob_data
                                if fee_note:
                                    fee_note = fee_note + "\n" + ob_depth_note
                                else:
                                    fee_note = ob_depth_note
                    except Exception:
                        pass

                    # ── Feature 5: Kalshi comment sentiment ──────────────────
                    if comments:
                        comment_text = " | ".join(comments[:5])
                        comment_note = f"Market comments (recent): {comment_text}"
                        fee_note = (fee_note + "\n" + comment_note) if fee_note else comment_note

                    # ── Feature 7: Polymarket copy-trading wallet signals ─────
                    if wallet_sig and (wallet_sig.get("YES", 0) + wallet_sig.get("NO", 0)) > 0:
                        yes_n = wallet_sig.get("YES", 0)
                        no_n  = wallet_sig.get("NO", 0)
                        vol   = wallet_sig.get("vol", 0.0)
                        total = yes_n + no_n
                        dominant = "YES" if yes_n >= no_n else "NO"
                        wallet_note = (f"Smart money (top leaderboard wallets, last 20min): "
                                       f"{yes_n}/{total} bought YES, {no_n}/{total} bought NO "
                                       f"${vol:.0f} total volume → {dominant} lean. "
                                       f"Wallets: {wallet_sig.get('wallets', [])}")
                        fee_note = (fee_note + "\n" + wallet_note) if fee_note else wallet_note

                    # 1. Try betbot autoresearch signal file first
                    bb_dir, bb_entry, bb_size = read_betbot_signal(coin, wts)
                    if bb_dir:
                        result = {"direction": bb_dir, "entry": bb_entry,
                                  "confidence": 0.75, "rationale": "autoresearch signal (evolved strategy)",
                                  "_betbot_size": bb_size}
                    else:
                        # 2. Use configured engine (minimax_llm / rules / knn / hybrid)
                        result = run_engine(get_engine_for_coin(coin), coin, mkt, ticks, coin_price, ticks_summary,
                                            market_volume=market_volume, spread=spread_v,
                                            poly_price=poly_price, prev_outcomes=prev_outcomes,
                                            fee_note=fee_note)

                    if not result:
                        # Fallback disabled — 160 fallback trades were net negative on every coin
                        conn.execute("""
                            INSERT OR IGNORE INTO decisions (coin, window_ts, direction, entry, confidence, rationale, decided_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (coin, wts, "PASS", 0.5, 0.0, "Engine returned no signal — fallback disabled", now_cst().isoformat()))
                        conn.commit()
                        print(f"[DECISION] {coin} wts={wts}: PASS (no engine signal)")
                        return

                    direction  = result.get("direction", "")
                    entry      = float(result.get("entry") or 0)
                    confidence = float(result.get("confidence", 0.5))
                    rationale  = result.get("rationale", "")
                    # ── Engine suggestion / auto-switch ─────────────────────────
                    sug_eng = result.get("suggest_engine")
                    VALID_ENGINES = ("rules_engine", "vector_knn", "hybrid", "minimax_llm")
                    AUTO_SWITCH_THRESHOLD = 8   # consecutive same suggestions to trigger switch
                    AUTO_SWITCH_BACK_WR   = 0.38 # if non-LLM engine WR drops below this, revert
                    try:
                        current_engine = get_engine_for_coin(coin)
                        # --- Auto-switch BACK to minimax_llm if non-LLM engine is underperforming ---
                        if current_engine != "minimax_llm":
                            wr_rows = conn.execute("""
                                SELECT result FROM paper_trades WHERE coin=? AND result IN ('WIN','LOSS')
                                ORDER BY window_ts DESC LIMIT 10
                            """, (coin,)).fetchall()
                            if len(wr_rows) >= 5:
                                wr = sum(1 for r in wr_rows if r[0]=='WIN') / len(wr_rows)
                                if wr < AUTO_SWITCH_BACK_WR:
                                    conn.execute("""
                                        INSERT OR REPLACE INTO market_group_engines (coin, engine_key, updated_at)
                                        VALUES (?, 'minimax_llm', ?)
                                    """, (coin, now_cst().isoformat()))
                                    conn.commit()
                                    # Reset suggestion streak
                                    conn.execute("DELETE FROM settings WHERE key=?", (f"engine_suggest_{coin}",))
                                    conn.commit()
                                    audit("engine_auto_switched", "market_group_engines", coin,
                                          {"from": current_engine, "to": "minimax_llm",
                                           "reason": f"WR {wr:.0%} < {AUTO_SWITCH_BACK_WR:.0%} over last {len(wr_rows)} trades"})
                                    print(f"[ENGINE] {coin}: auto-reverted to minimax_llm (WR {wr:.0%} on {current_engine})")
                        # --- Track suggestion streak and auto-switch forward ---
                        if sug_eng and sug_eng in VALID_ENGINES and sug_eng != current_engine:
                            streak_row = conn.execute(
                                "SELECT value FROM settings WHERE key=?", (f"engine_suggest_{coin}",)
                            ).fetchone()
                            # value format: "engine_key:count"
                            if streak_row and streak_row[0].startswith(sug_eng + ":"):
                                streak = int(streak_row[0].split(":")[1]) + 1
                            else:
                                streak = 1
                            conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?,?,?)",
                                         (f"engine_suggest_{coin}", f"{sug_eng}:{streak}", now_cst().isoformat()))
                            conn.commit()
                            rationale = rationale.rstrip(".") + f" [suggests: {sug_eng} {streak}/{AUTO_SWITCH_THRESHOLD}]"
                            if streak >= AUTO_SWITCH_THRESHOLD:
                                conn.execute("""
                                    INSERT OR REPLACE INTO market_group_engines (coin, engine_key, updated_at)
                                    VALUES (?, ?, ?)
                                """, (coin, sug_eng, now_cst().isoformat()))
                                conn.commit()
                                conn.execute("DELETE FROM settings WHERE key=?", (f"engine_suggest_{coin}",))
                                conn.commit()
                                audit("engine_auto_switched", "market_group_engines", coin,
                                      {"from": current_engine, "to": sug_eng,
                                       "reason": f"LLM suggested {AUTO_SWITCH_THRESHOLD} consecutive windows"})
                                print(f"[ENGINE] {coin}: auto-switched {current_engine} → {sug_eng} (LLM suggested {AUTO_SWITCH_THRESHOLD}x)")
                        elif not sug_eng or sug_eng == current_engine:
                            # Clear streak if LLM stops suggesting or agrees with current engine
                            conn.execute("DELETE FROM settings WHERE key=?", (f"engine_suggest_{coin}",))
                            conn.commit()
                    except Exception as _se:
                        print(f"[ENGINE SUGGEST] {coin}: {_se}")
                    if direction not in ("YES", "NO") or entry <= 0 or entry >= 1.0:
                        print(f"[SKIP] {coin} wts={wts}: bad engine result dir={direction} entry={entry}")
                        return

                    # ── Signal quality filters (data-driven) ────────────────────
                    # Filter 1: minimum confidence — conf<=0.5 is 47% WR / -$535 in data
                    try:
                        min_conf = float(conn.execute(
                            "SELECT value FROM settings WHERE key='min_confidence'"
                        ).fetchone()[0])
                    except Exception:
                        min_conf = 0.55
                    is_betbot = bool(result.get("_betbot_size"))
                    if not is_betbot and confidence < min_conf:
                        conn.execute("""
                            INSERT OR IGNORE INTO decisions (coin, window_ts, direction, entry, confidence, rationale, decided_at)
                            VALUES (?,?,?,?,?,?,?)
                        """, (coin, wts, "PASS", round(entry, 4), confidence,
                              f"Conf PASS: {confidence:.2f} <= min {min_conf:.2f}", now_cst().isoformat()))
                        conn.commit()
                        print(f"[FILTER] {coin} wts={wts}: PASS — confidence {confidence:.2f} below min {min_conf:.2f}")
                        return

                    # Filter 2: SOL YES above 0.55 entry — 36-43% WR at every bucket above that
                    if coin == "SOL" and direction == "YES" and entry > 0.55 and not is_betbot:
                        conn.execute("""
                            INSERT OR IGNORE INTO decisions (coin, window_ts, direction, entry, confidence, rationale, decided_at)
                            VALUES (?,?,?,?,?,?,?)
                        """, (coin, wts, "PASS", round(entry, 4), confidence,
                              f"Bias PASS: SOL YES entry {entry:.3f} > 0.55 (historically 36-43% WR)", now_cst().isoformat()))
                        conn.commit()
                        print(f"[FILTER] SOL wts={wts}: PASS — YES entry {entry:.3f} blocked (bias filter)")
                        return

                    # Risk check + variable stake
                    acct_pre = conn.execute("SELECT capital FROM paper_accounts WHERE coin=?", (coin,)).fetchone()
                    capital_pre = acct_pre[0] if acct_pre else STARTING_CAPITAL
                    betbot_size = result.get("_betbot_size")
                    if betbot_size and betbot_size > 0:
                        size = min(float(betbot_size), capital_pre * 0.10)
                    else:
                        size = calc_stake(coin, confidence, capital_pre, entry=entry)
                    ok, reason = check_risk(coin, direction, entry, size)
                    if not ok:
                        print(f"[RISK] {coin}: blocked — {reason}")
                        conn.execute("""
                            INSERT OR IGNORE INTO decisions (coin, window_ts, direction, entry, confidence, rationale, decided_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (coin, wts, "PASS", round(entry, 4), confidence,
                              f"Risk block: {reason}", now_cst().isoformat()))

                        conn.commit()
                        return

                    conn.execute("""
                        INSERT OR IGNORE INTO decisions (coin, window_ts, direction, entry, confidence, rationale, decided_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (coin, wts, direction, round(entry, 4), confidence, rationale, now_cst().isoformat()))

                    contracts = min(size / entry, MAX_CONTRACTS)
                    size = contracts * entry  # recalculate actual cost after cap

                    # Live liquidity check against Kalshi order book
                    ticker = mkt.get("ticker", "")
                    available, ob_ok = get_available_contracts(ticker, direction, entry)
                    requested_contracts = contracts
                    liquidity_ok = True
                    if ob_ok and available is not None:
                        if available < 10:
                            # Not enough liquidity — log and skip
                            conn.execute("""
                                INSERT INTO fill_quality (coin, window_ts, ticker, direction, entry,
                                    requested_contracts, available_contracts, filled_contracts,
                                    liquidity_ok, created_at)
                                VALUES (?,?,?,?,?,?,?,?,?,?)
                            """, (coin, wts, ticker, direction, round(entry,4),
                                  round(requested_contracts,2), round(available,2), 0, 0,
                                  now_cst().isoformat()))
                            conn.execute("""
                                INSERT OR IGNORE INTO decisions (coin, window_ts, direction, entry, confidence, rationale, decided_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                            """, (coin, wts, "PASS", round(entry,4), confidence,
                                  f"Liquidity PASS: only {available:.0f} contracts available at {entry:.3f}",
                                  now_cst().isoformat()))
                            conn.commit()
                            print(f"[LIQUIDITY] {coin} wts={wts}: PASS — only {available:.0f} contracts at {entry:.3f}")
                            return
                        # Reduce to available if less than requested
                        if available < contracts:
                            contracts = available
                            size = contracts * entry
                            liquidity_ok = False  # partial fill
                    # Log fill quality
                    try:
                        conn.execute("""
                            INSERT INTO fill_quality (coin, window_ts, ticker, direction, entry,
                                requested_contracts, available_contracts, filled_contracts,
                                liquidity_ok, created_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?)
                        """, (coin, wts, ticker, direction, round(entry,4),
                              round(requested_contracts,2), round(available,2) if available is not None else None,
                              round(contracts,2), 1 if liquidity_ok else 0, now_cst().isoformat()))
                    except Exception:
                        pass

                    run_id = get_active_run_id(coin, conn)

                    conn.execute("""
                        INSERT OR IGNORE INTO paper_trades
                        (coin, run_id, window_ts, direction, entry, size, contracts, coin_open, decided_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (coin, run_id, wts, direction, round(entry, 4), round(size, 4),
                          round(contracts, 4), coin_price, now_cst().isoformat()))

                    conn.commit()
                    liq_note = f"  avail={available:.0f}" if available is not None else "  (ob unavail)"
                    print(f"[DECISION] {coin} wts={wts}: {direction} @ {entry:.3f}  conf={confidence:.2f}  contracts={contracts:.1f}{liq_note}  '{rationale[:50]}'")

                    # ── Live order placement ─────────────────────────────────
                    try:
                        conn2 = db_connect()
                        global_live = conn2.execute("SELECT global_live_enabled FROM system_state WHERE id=1").fetchone()
                        coin_mode_row = conn2.execute("SELECT mode FROM coin_modes WHERE coin=?", (coin,)).fetchone()
                        pool_row = conn2.execute("SELECT value FROM settings WHERE key='pool_mode'").fetchone()
                        conn2.close()
                        is_live = (global_live and global_live[0]) and (coin_mode_row and coin_mode_row[0] == "live")
                        pool_on = pool_row and pool_row[0] == "1"
                        if is_live and ticker:
                            live_contracts = max(1, int(contracts))
                            if pool_on and pool_signals is not None:
                                # Pool mode: register signal, winner picked after all threads done
                                with pool_lock:
                                    pool_signals[coin] = {
                                        'direction': direction, 'entry': entry,
                                        'confidence': confidence, 'contracts': live_contracts,
                                        'ticker': ticker
                                    }
                                print(f"[POOL] {coin} signal registered: {direction}@{entry:.3f} conf={confidence:.2f}")
                            else:
                                # Normal mode: place order immediately
                                order_id, used_ticker, order_err = place_kalshi_order(coin, ticker, direction, live_contracts, entry)
                                conn3 = db_connect()
                                conn3.execute("""
                                    INSERT INTO live_orders (coin, window_ts, ticker, direction, contracts,
                                        limit_price, order_id, status, error, created_at)
                                    VALUES (?,?,?,?,?,?,?,?,?,?)
                                """, (coin, wts, used_ticker, direction, live_contracts,
                                      max(1, min(99, round(entry*100))),
                                      order_id, "placed" if order_id else "failed",
                                      order_err, now_cst().isoformat()))
                                conn3.commit()
                                conn3.close()
                                if order_id:
                                    print(f"[LIVE] {coin} order placed: {order_id}  {direction} x{live_contracts} @ {entry:.3f}")
                                else:
                                    print(f"[LIVE] {coin} order FAILED: {order_err}")
                    except Exception as le:
                        print(f"[LIVE] {coin} order exception: {le}")
                except Exception as e:
                    print(f"[DECISION] {coin}: {e}")
                    traceback.print_exc()
                finally:
                    conn.close()
              except Exception as e:
                print(f"[DECISION] {coin} outer: {e}")

            # Retry loop: if all coins have stale data, wait for Kalshi collector to catch up
            _retry_deadline = wts + 175  # give up 5s before the 180s cutoff
            while True:
                _stale_counter = []
                # Pool mode: collect live signals, pick best one after all threads done
                _pool_signals = {}  # coin -> {direction, entry, confidence, contracts, ticker}
                _pool_lock = threading.Lock()

                def _decide_coin_pooled(coin, wts, now, api_delay=0):
                    _decide_coin(coin, wts, now, pool_signals=_pool_signals, pool_lock=_pool_lock,
                                 stale_counter=_stale_counter, api_delay=api_delay)

                # All coins check market data immediately; stagger only the MiniMax API call
                threads = [threading.Thread(target=_decide_coin_pooled,
                           args=(coin, wts, now, i * 10), daemon=True)
                           for i, coin in enumerate(COINS)]
                for t in threads: t.start()
                for t in threads: t.join(timeout=180)

                # If any coin was stale and we still have time, wait and retry
                if len(_stale_counter) > 0 and int(time.time()) < _retry_deadline:
                    print(f"[DECISION] {len(_stale_counter)} coins stale for wts={wts}, waiting 15s for Kalshi collector...")
                    time.sleep(15)
                    continue
                break  # all coins got fresh data, or window closing

            # ── Pool mode: pick best live signal and place one order ──────────
            try:
                conn_pool = db_connect()
                pool_enabled = conn_pool.execute(
                    "SELECT value FROM settings WHERE key='pool_mode'"
                ).fetchone()
                global_live = conn_pool.execute(
                    "SELECT global_live_enabled FROM system_state WHERE id=1"
                ).fetchone()
                conn_pool.close()
                pool_on = pool_enabled and pool_enabled[0] == "1"
                live_on = global_live and global_live[0]
                if pool_on and live_on and _pool_signals and _pool_last_placed_wts < wts:
                    # Get rolling win rate for scoring
                    conn_pool2 = db_connect()
                    # Feature 10: multi-position pool threshold
                    try:
                        pool_thresh_row = conn_pool2.execute(
                            "SELECT value FROM settings WHERE key='pool_multi_threshold'"
                        ).fetchone()
                        pool_multi_threshold = float(pool_thresh_row[0]) if pool_thresh_row else 0.0
                    except Exception:
                        pool_multi_threshold = 0.0
                    def rolling_wr(coin):
                        rows = conn_pool2.execute(
                            "SELECT result FROM paper_trades WHERE coin=? AND result IN ('WIN','LOSS') ORDER BY window_ts DESC LIMIT 10",
                            (coin,)
                        ).fetchall()
                        if len(rows) < 3: return 0.5
                        return sum(1 for r in rows if r[0]=='WIN') / len(rows)
                    # Score = confidence * 0.7 + rolling_wr * 0.3
                    scored = sorted(
                        [(c, _pool_signals[c]['confidence'] * 0.7 + rolling_wr(c) * 0.3) for c in _pool_signals],
                        key=lambda x: x[1], reverse=True
                    )
                    conn_pool2.close()
                    # Feature 3: Correlated coin dedup — keep only best per group
                    CORR_GROUPS = [{"BTC", "ETH"}, {"SOL", "XRP", "DOGE", "BNB", "HYPE"}]
                    seen_groups = set()
                    deduped = []
                    for c, s in scored:
                        grp = next((i for i, g in enumerate(CORR_GROUPS) if c in g), -1)
                        if grp >= 0 and grp in seen_groups:
                            print(f"[POOL] {c} skipped (correlated group {grp} already represented)")
                            continue
                        if grp >= 0:
                            seen_groups.add(grp)
                        deduped.append((c, s))
                    scored = deduped
                    # If multi-threshold set and > 0, place all coins scoring above it; otherwise just winner
                    if pool_multi_threshold > 0:
                        pool_winners = [(c, s) for c, s in scored if s >= pool_multi_threshold]
                        if not pool_winners:
                            pool_winners = [scored[0]]  # always place at least winner
                    else:
                        pool_winners = [scored[0]]
                    skipped = [c for c, _ in scored if c not in [w for w, _ in pool_winners]]
                    for best_coin, best_score in pool_winners:
                        sig = _pool_signals[best_coin]
                        order_id, used_ticker, order_err = place_kalshi_order(
                            best_coin, sig['ticker'], sig['direction'], sig['contracts'], sig['entry']
                        )
                        conn_pool3 = db_connect()
                        conn_pool3.execute("""
                            INSERT INTO live_orders (coin, window_ts, ticker, direction, contracts,
                                limit_price, order_id, status, error, created_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?)
                        """, (best_coin, wts, used_ticker, sig['direction'], sig['contracts'],
                              max(1, min(99, round(sig['entry']*100))),
                              order_id, "placed" if order_id else "failed",
                              order_err, now_cst().isoformat()))
                        conn_pool3.commit()
                        conn_pool3.close()
                        print(f"[POOL] {best_coin} {sig['direction']}@{sig['entry']:.3f} conf={sig['confidence']:.2f} score={best_score:.2f}  order={'OK' if order_id else 'FAIL'}")
                    _pool_last_placed_wts = wts
                    if skipped:
                        print(f"[POOL] skipped (below threshold {pool_multi_threshold:.2f}): {skipped}")
            except Exception as pe:
                print(f"[POOL] {pe}")
            last_decided_wts = wts
            try:
                resolve_trades()
            except Exception as e:
                print(f"[RESOLVE] {e}")
            try:
                resolve_live_orders()
            except Exception as e:
                print(f"[LIVE RESOLVE] {e}")
        except Exception as e:
            print(f"[DECISION LOOP] {e}")
        time.sleep(30)

# ── Betbot signals bridge ───────────────────────────────────────────────────────
BETBOT_SIGNAL_FILES = {
    "BTC":  "/home/sean/autoresearch/data/kalshi_signals.json",
    "ETH":  "/home/sean/autoresearch/data/kalshi_signals_eth.json",
    "SOL":  "/home/sean/autoresearch/data/kalshi_signals_sol.json",
    "XRP":  "/home/sean/autoresearch/data/kalshi_signals_xrp.json",
    "DOGE": "/home/sean/autoresearch/data/kalshi_signals_doge.json",
    "BNB":  "/home/sean/autoresearch/data/kalshi_signals_bnb.json",
    "HYPE": "/home/sean/autoresearch/data/kalshi_signals_hype.json",
}
_betbot_signals_cache: dict = {}

def read_betbot_signal(coin, window_ts):
    path = BETBOT_SIGNAL_FILES.get(coin)
    if not path:
        return None, None, None
    try:
        mtime = os.path.getmtime(path)
        cached = _betbot_signals_cache.get(coin)
        if cached and cached[0] == mtime:
            signals = cached[1]
        else:
            with open(path) as f:
                signals = json.load(f)
            _betbot_signals_cache[coin] = (mtime, signals)
        sig = signals.get(str(window_ts))
        if sig and sig.get("dir") in ("YES", "NO") and sig.get("entry", 0) > 0:
            return sig["dir"], float(sig["entry"]), float(sig.get("size", TRADE_SIZE))
    except Exception:
        pass
    return None, None, None


# ── Decision engine registry ────────────────────────────────────────────────────
def get_engine_for_coin(coin):
    try:
        conn = db_connect()
        row = conn.execute("SELECT engine_key FROM market_group_engines WHERE coin=?", (coin,)).fetchone()
        conn.close()
        return row[0] if row else "minimax_llm"
    except Exception:
        return "minimax_llm"

def run_engine(engine_key, coin, mkt, ticks, coin_price, ticks_summary, market_volume=None, spread=None,
               poly_price=None, prev_outcomes=None, fee_note=None):
    if engine_key == "rules_engine":
        return rules_engine(coin, mkt)
    elif engine_key == "vector_knn":
        return vector_knn_engine(coin, mkt, ticks)
    elif engine_key == "hybrid":
        return hybrid_engine(coin, mkt, ticks)
    else:  # minimax_llm — run all engines first, synthesize with LLM
        rules_sig = rules_engine(coin, mkt)
        knn_sig   = vector_knn_engine(coin, mkt, ticks)
        # Price momentum: % change over last 5 minutes from tick coin_price
        price_momentum = None
        try:
            prices = [float(t[3]) for t in ticks if t[3]]  # ticks: (wts, yes_bid, yes_ask, coin_price, secs_left)
            if len(prices) >= 2:
                price_momentum = (prices[-1] - prices[0]) / prices[0]
        except Exception:
            pass
        return minimax_analyze(coin, ticks_summary, coin_price,
                               market_volume=market_volume, spread=spread,
                               rules_signal=rules_sig, knn_signal=knn_sig,
                               price_momentum=price_momentum,
                               poly_price=poly_price, prev_outcomes=prev_outcomes,
                               fee_note=fee_note)

def rules_engine(coin, mkt):
    try:
        yes_ask = float(mkt.get("yes_ask", 0) or 0)
        yes_bid = float(mkt.get("yes_bid", 0) or 0)
        if yes_ask <= 0 or yes_bid <= 0:
            return None
        mid    = (yes_bid + yes_ask) / 2
        spread = yes_ask - yes_bid
        if spread > 0.15:
            return None
        conf = abs(mid - 0.5) * 2
        if mid > 0.62:
            return {"direction": "YES", "entry": yes_ask,
                    "confidence": round(conf, 4), "rationale": f"Rules: mid={mid:.3f}>0.62"}
        elif mid < 0.38:
            return {"direction": "NO", "entry": round(1.0 - yes_bid, 4),
                    "confidence": round(conf, 4), "rationale": f"Rules: mid={mid:.3f}<0.38"}
    except Exception:
        pass
    return None

def vector_knn_engine(coin, mkt, ticks, k=10):
    """KNN: build 8-feature vector for current window, find K nearest historical
    neighbors in kalshi_ticks, vote on direction by their actual outcomes."""
    try:
        import math
        yes_ask = float(mkt.get("yes_ask", 0) or 0)
        yes_bid = float(mkt.get("yes_bid", 0) or 0)
        if yes_ask <= 0 or yes_bid <= 0:
            return None
        mid    = (yes_bid + yes_ask) / 2
        spread = yes_ask - yes_bid

        # Build query feature vector (8 dims)
        coin_price = mkt.get("coin_price") or 0
        secs_left  = float(mkt.get("secs_left", 450) or 450)
        tick_momentum = 0.0
        if len(ticks) >= 2:
            try:
                mids = [(float(t.get("yes_bid",0))+float(t.get("yes_ask",0)))/2 for t in ticks[-5:] if t.get("yes_ask")]
                if len(mids) >= 2:
                    tick_momentum = mids[-1] - mids[0]
            except Exception:
                pass

        query = [mid, spread, secs_left/900.0, tick_momentum,
                 yes_ask, yes_bid, (mid-0.5)**2, abs(tick_momentum)]

        # Load historical windows with known outcomes from paper_trades
        conn = db_connect()
        hist = conn.execute("""
            SELECT t.yes_bid, t.yes_ask, t.secs_left, pt.actual, d.entry,
                   t.coin_price, t.window_ts
            FROM kalshi_ticks t
            JOIN decisions d ON d.coin=t.coin AND d.window_ts=t.window_ts
            JOIN paper_trades pt ON pt.coin=t.coin AND pt.window_ts=t.window_ts
            WHERE t.coin=? AND d.direction IN ('YES','NO')
              AND pt.result IN ('WIN','LOSS') AND pt.actual IN ('YES','NO')
            ORDER BY t.ts DESC LIMIT 2000
        """, (coin,)).fetchall()
        conn.close()

        if len(hist) < 20:
            return rules_engine(coin, mkt)  # not enough history, fall back

        # Compute cosine similarity for each historical row
        def dot(a, b): return sum(x*y for x,y in zip(a,b))
        def norm(a): return math.sqrt(sum(x*x for x in a)) or 1e-9

        scored = []
        for row in hist:
            hb, ha, hs, hdir, he, hcp, hwts = row
            hb = float(hb or 0); ha = float(ha or 0)
            if ha <= 0 or hb <= 0: continue
            hmid   = (hb + ha) / 2
            hspread= ha - hb
            hmomentum = 0.0
            hvec   = [hmid, hspread, float(hs or 450)/900.0, hmomentum,
                      ha, hb, (hmid-0.5)**2, abs(hmomentum)]
            sim = dot(query, hvec) / (norm(query) * norm(hvec))
            scored.append((sim, hdir, he))

        scored.sort(reverse=True)
        neighbors = scored[:k]
        yes_votes = sum(1 for _, d, _ in neighbors if d == "YES")
        no_votes  = k - yes_votes
        confidence = max(yes_votes, no_votes) / k

        if yes_votes > no_votes:
            avg_entry = sum(e for _, d, e in neighbors if d == "YES") / yes_votes
            return {"direction": "YES", "entry": min(round(avg_entry, 4), yes_ask),
                    "confidence": round(confidence, 4),
                    "rationale": f"KNN({k}): {yes_votes}/{k} YES neighbors"}
        elif no_votes > yes_votes:
            avg_entry = sum(e for _, d, e in neighbors if d == "NO") / no_votes
            return {"direction": "NO", "entry": min(round(avg_entry, 4), round(1-yes_bid, 4)),
                    "confidence": round(confidence, 4),
                    "rationale": f"KNN({k}): {no_votes}/{k} NO neighbors"}
    except Exception as e:
        print(f"[KNN] {coin}: {e}")
    return None

def hybrid_engine(coin, mkt, ticks):
    """Rules gate first; if signal, refine confidence with KNN."""
    rules = rules_engine(coin, mkt)
    if not rules:
        return None
    knn = vector_knn_engine(coin, mkt, ticks)
    if knn and knn.get("direction") == rules.get("direction"):
        avg_conf = (rules["confidence"] + knn["confidence"]) / 2
        rules["confidence"] = round(avg_conf, 4)
        rules["rationale"] += " + KNN"
    return rules


# ── Replay engine ────────────────────────────────────────────────────────────────
def run_replay(coin, engine_key, start_ts, end_ts, starting_capital=100.0, run_name=None):
    """Execute a replay run against historical kalshi_ticks data.
    Returns replay_run_id."""
    conn = db_connect()
    name = run_name or f"{coin} {engine_key} replay {ts_cst(start_ts).strftime('%m/%d')}"
    conn.execute("""
        INSERT INTO replay_runs (name, coin, engine_key, start_ts, end_ts, starting_capital, status, created_at)
        VALUES (?,?,?,?,?,?,'running',?)
    """, (name, coin, engine_key, start_ts, end_ts, starting_capital, now_cst().isoformat()))
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.commit()

    # Load historical windows
    windows = conn.execute("""
        SELECT DISTINCT window_ts FROM kalshi_ticks
        WHERE coin=? AND window_ts BETWEEN ? AND ?
        ORDER BY window_ts
    """, (coin, start_ts, end_ts)).fetchall()

    balance = starting_capital
    wins = losses = 0

    for (wts,) in windows:
        # Get the opening tick for this window (lowest secs_left = earliest in window)
        ticks_rows = conn.execute("""
            SELECT yes_bid, yes_ask, secs_left, coin_price FROM kalshi_ticks
            WHERE coin=? AND window_ts=? ORDER BY secs_left DESC
        """, (coin, wts)).fetchall()
        if not ticks_rows:
            continue
        open_row = ticks_rows[0]
        close_row = ticks_rows[-1]

        mkt = {"yes_bid": open_row[0], "yes_ask": open_row[1],
               "secs_left": open_row[2], "coin_price": open_row[3], "window_ts": wts}
        coin_price_open  = open_row[3] or 0
        coin_price_close = close_row[3] or 0

        ticks_dicts = [{"yes_bid": r[0], "yes_ask": r[1], "secs_left": r[2]} for r in ticks_rows[:10]]

        # Get engine decision
        if engine_key == "betbot_signal":
            bb_dir, bb_entry, _ = read_betbot_signal(coin, wts)
            if not bb_dir:
                continue
            result = {"direction": bb_dir, "entry": bb_entry, "confidence": 0.75}
        elif engine_key == "rules_engine":
            result = rules_engine(coin, mkt)
        elif engine_key == "vector_knn":
            result = vector_knn_engine(coin, mkt, ticks_dicts)
        elif engine_key == "hybrid":
            result = hybrid_engine(coin, mkt, ticks_dicts)
        else:
            continue  # skip minimax_llm in replay (no API calls)

        if not result:
            continue
        direction = result.get("direction","")
        entry     = float(result.get("entry", 0))
        if direction not in ("YES","NO") or entry <= 0 or entry >= 1.0:
            continue
        if entry < ENTRY_FLOOR or entry > ENTRY_CEILING:
            continue

        # Actual outcome: was price higher at close?
        if coin_price_open <= 0 or coin_price_close <= 0:
            continue
        actual = "YES" if coin_price_close > coin_price_open else "NO"

        size      = min(20.0, balance * 0.10)
        contracts = min(size / entry, MAX_CONTRACTS)
        size      = contracts * entry  # actual cost after cap
        if direction == actual:
            profit = contracts * (1.0 - entry)
            fee = contracts * min(KALSHI_FEE_RATE * entry * (1.0 - entry), 0.02)
            net = profit - fee
            result_str = "WIN"; wins += 1
        else:
            net = -(contracts * entry)
            fee = 0.0; result_str = "LOSS"; losses += 1

        balance += net
        conn.execute("""
            INSERT INTO replay_trades
            (replay_run_id, coin, window_ts, engine_key, direction, entry, size, contracts,
             pnl, result, balance, decided_at, resolved_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (run_id, coin, wts, engine_key, direction, round(entry,4), round(size,4),
              round(contracts,4), round(net,4), result_str, round(balance,2),
              ts_cst(wts).isoformat(), ts_cst(wts+900).isoformat()))

    total = wins + losses
    win_rate = wins/total if total else 0
    conn.execute("""
        UPDATE replay_runs SET status='done' WHERE id=?
    """, (run_id,))

    conn.commit()
    conn.close()
    print(f"[REPLAY] run_id={run_id} {coin} {engine_key}: {wins}W/{losses}L  bal=${balance:.2f}")
    return run_id


# ── Dataset import helpers ───────────────────────────────────────────────────────
def run_import_job(job_id, source, file_path, coin=None):
    """Background worker for dataset import jobs."""
    conn = db_connect()
    def set_status(status, error=None, count=0):
        conn.execute("""UPDATE import_jobs SET status=?, records_imported=?, error_msg=?, completed_at=?
                        WHERE id=?""",
                     (status, count, error, now_cst().isoformat(), job_id))

        conn.commit()
    try:
        if source == "kalshi_csv":
            import csv as _csv
            count = 0
            with open(file_path, newline="") as f:
                reader = _csv.DictReader(f)
                for row in reader:
                    try:
                        c = (coin or row.get("coin","BTC")).upper()
                        conn.execute("""
                            INSERT OR IGNORE INTO kalshi_ticks
                            (coin, window_ts, market_ticker, yes_bid, yes_ask, last_price, secs_left, coin_price, ts)
                            VALUES (?,?,?,?,?,?,?,?,?)
                        """, (c, int(row["window_ts"]), row.get("market_ticker",""),
                              float(row.get("yes_bid",0)), float(row.get("yes_ask",0)),
                              float(row.get("last_price",0)), float(row.get("secs_left",0)),
                              float(row.get("coin_price",0) or row.get("btc_price",0)),
                              int(row.get("ts", row.get("window_ts",0)))))
                        count += 1
                    except Exception:
                        pass

            conn.commit()
            set_status("done", count=count)
        elif source == "price_csv":
            import csv as _csv
            count = 0
            with open(file_path, newline="") as f:
                reader = _csv.DictReader(f)
                for row in reader:
                    try:
                        c = (coin or "BTC").upper()
                        conn.execute("INSERT OR IGNORE INTO price_history (coin,price,ts) VALUES (?,?,?)",
                                     (c, float(row.get("price",0) or row.get("btc_price",0)),
                                      int(row.get("ts",0))))
                        count += 1
                    except Exception:
                        pass

            conn.commit()
            set_status("done", count=count)
        else:
            set_status("error", error=f"Unknown source: {source}")
    except Exception as e:
        set_status("error", error=str(e))
    finally:
        conn.close()


# ── Tooltips ────────────────────────────────────────────────────────────────────
TOOLTIPS = {
    "execution_mode":       "observe=collect only; paper=simulate trades; live=real orders (requires global live toggle)",
    "paper_mode":           "Simulated trading using real market data. No real money at risk.",
    "live_mode":            "Real order placement. Requires global live toggle ON plus per-coin mode = live.",
    "observe_mode":         "Data collection only. No trades placed. Use to gather history before trading.",
    "trade_venue":          "The venue where actual orders are placed (Kalshi, Polymarket).",
    "reference_authority":  "Outside price source used to determine outcomes (e.g. Coinbase for crypto).",
    "decision_engine":      "The strategy that converts market data into YES/NO/PASS recommendations.",
    "paper_profile":        "Settings for a paper trading experiment: starting capital, stake size, fee mode.",
    "fee_profile":          "How trading fees are estimated. Kalshi: 7% of profit, capped at $0.02/contract.",
    "edge":                 "Expected profit advantage after fees. Positive edge = trade is theoretically profitable.",
    "confidence":           "Model's certainty in the prediction. Higher = stronger signal.",
    "kill_switch":          "Emergency stop. When ON, all new trades are blocked immediately.",
    "daily_loss_limit":     "Maximum total loss per coin per day before trading is suspended.",
    "max_drawdown_pct":     "Maximum allowed drop from starting capital before trading stops (e.g. 30% = stop at $350 from $500).",
    "global_live_enabled":  "Master toggle for live trading. OFF by default. Both this AND per-coin mode must be live.",
    "provider":             "An API service (Kalshi, Coinbase, MiniMax) used for data or decisions.",
    "market_group":         "A tradeable coin/asset group (BTC, ETH, SOL, XRP) with its own config.",
    "paper_run":            "An isolated experiment session with its own capital and trade history.",
    "replay_mode":          "Replay historical data as if it's live, to test strategies without lookahead bias.",
    "cooldown_after_losses": "Number of consecutive losses before a coin enters cooldown (pauses new trades).",
    # Insights page
    "drill_down":           "Filter this page to show only data for one coin. Click 'All' to return to all-coin view.",
    "edge_pct":             "Edge% = win_rate × avg_win_profit − loss_rate × avg_loss. Positive = you have a theoretical profit edge at this confidence/entry level. Negative = you're giving money away.",
    "confidence_calibration": "How well the model's confidence score predicts actual win rate. If 80% confidence → 80% win rate, calibration is perfect. A big gap means the model is over- or under-confident.",
    "entry_vs_edge":        "Your entry price affects profitability. Lower entry = higher payout if you win but harder to win. EV (expected value) after Kalshi fees shown per entry price bucket.",
    "ev":                   "Expected Value: the average profit per $1 risked. EV > 0 means you should trade this bucket; EV < 0 means avoid it even with a high win rate (fees eat the edge).",
    "prob_bar":             "Visual indicator of Kalshi market probability. Shaded region = bid→ask spread. White line = midpoint. Blue triangle = Polymarket price. Green = YES-leaning (>55%), Red = NO-leaning (<45%).",
    "ex_outlier":           "P&L excluding the top 3 biggest wins. Strips lucky outlier trades to show the underlying strategy performance. Negative = the strategy loses money without lucky strikes.",
    # Engine manager
    "engine_minimax_llm":   "Calls MiniMax API each window with tick history + coin price as context. Highest quality, uses API tokens (~$0.001/call). Best when you have reliable API access.",
    "engine_rules":         "Pure rules: if Kalshi mid > 0.62 bet YES, if mid < 0.38 bet NO, else skip. Zero API calls. Fast. Works best in strongly trending markets. May overtrade in sideways conditions.",
    "engine_knn":           "Vector KNN: builds an 8-feature vector (mid, spread, momentum, secs_left…) and finds the K=10 most similar historical windows. Votes direction by what those windows did. Requires 20+ resolved decisions in DB.",
    "engine_hybrid":        "Runs the rules engine first as a gate — if it says PASS, no trade. If it says YES/NO, runs KNN to adjust confidence. Best of both: rules filter + data-driven sizing.",
    "engine_betbot_signal": "Reads the evolved strategy signal files that betbot's autoresearch loop writes. The loop uses MiniMax M2.7 to rewrite kalshi_analyze.py every few windows based on P&L feedback. Most sophisticated option when the loop is running.",
    # Replay
    "replay":               "Run any engine against your collected historical tick data. No API calls are made — the engine runs purely on recorded market snapshots. Use this to compare engines before switching live.",
    # Run reset
    "run_reset":            "Archives the current paper run (saves its history) and starts a new one at the configured starting capital. Also resets the daily-loss and cooldown guardrail counters for this coin. The kill switch and loss limits themselves are NOT changed.",
}

def tooltip_html(key):
    text = TOOLTIPS.get(key, "")
    if not text:
        return ""
    escaped = text.replace('"', '&quot;')
    return f'<span class="tt" data-tip="{escaped}">ⓘ</span>'

def prob_bar(yes_bid, yes_ask, poly_price=None):
    """Render a visual probability bar showing Kalshi bid/ask spread and Polymarket marker.
    The bar shows the full 0–1 range. The shaded region = bid→ask (the spread).
    A triangle marker shows Polymarket's price if available."""
    try:
        yb = float(yes_bid or 0)
        ya = float(yes_ask or 0)
        if ya <= 0 or yb < 0 or yb > ya:
            return ""
        mid = (yb + ya) / 2
        # Color: green for YES-leaning (>0.55), red for NO-leaning (<0.45), gray neutral
        if mid > 0.55:
            spread_color = "#238636"
        elif mid < 0.45:
            spread_color = "#da3633"
        else:
            spread_color = "#555"
        bid_pct  = int(yb * 100)
        ask_pct  = int(ya * 100)
        mid_pct  = int(mid * 100)
        spread_w = ask_pct - bid_pct
        poly_marker = ""
        if poly_price:
            pp = float(poly_price)
            poly_marker = f'<div style="position:absolute;left:{int(pp*100)}%;top:-3px;width:2px;height:calc(100%+6px);background:#58a6ff;z-index:2" title="Polymarket: {pp:.3f}"></div>'
        poly_title = f" | Polymarket: {float(poly_price):.3f}" if poly_price else ""
        return f'''<div style="position:relative;height:10px;background:#21262d;border-radius:4px;overflow:hidden" title="Kalshi: bid={yb:.3f} ask={ya:.3f} mid={mid:.3f}{poly_title}">
  <div style="position:absolute;left:{bid_pct}%;width:{spread_w}%;height:100%;background:{spread_color};opacity:0.7;border-radius:3px"></div>
  <div style="position:absolute;left:{mid_pct}%;top:0;width:2px;height:100%;background:#fff;opacity:0.6"></div>
  {poly_marker}
</div><div style="font-size:10px;color:#8b949e;text-align:right;margin-top:1px">{mid_pct}%</div>'''
    except Exception:
        return ""

TOOLTIP_CSS = """
  .tt { color: #58a6ff; cursor: help; font-size: 11px; margin-left: 4px; user-select: none; position: relative; display: inline-block; }
  .tt:hover::after {
    content: attr(data-tip);
    position: absolute;
    left: 50%;
    top: calc(100% + 4px);
    transform: translateX(-50%);
    background: #1c2128;
    border: 1px solid #444;
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 12px;
    color: #e6edf3;
    min-width: 200px;
    max-width: 320px;
    white-space: normal;
    z-index: 9999;
    line-height: 1.5;
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
    pointer-events: none;
  }
"""

# ── Shared page chrome ──────────────────────────────────────────────────────────
SHARED_CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; min-height: 100vh; }
  a { color: inherit; text-decoration: none; }
  .topbar { background: #161b22; border-bottom: 1px solid #30363d; padding: 0 12px; display: flex; align-items: center; height: 52px; gap: 0; position: sticky; top: 0; z-index: 100; }
  .logo-img { height: 36px; width: 36px; border-radius: 8px; object-fit: cover; margin-right: 10px; flex-shrink: 0; }
  .logo-text { font-size: 16px; font-weight: 800; color: #58a6ff; letter-spacing: 2px; margin-right: 12px; white-space: nowrap; flex-shrink: 0; }
  .nav { display: flex; gap: 2px; flex: 1; overflow-x: auto; scrollbar-width: none; -webkit-overflow-scrolling: touch; }
  .nav::-webkit-scrollbar { display: none; }
  .nav a { padding: 7px 10px; border-radius: 6px; font-size: 12px; font-weight: 500; color: #8b949e; transition: background 0.15s, color 0.15s; white-space: nowrap; }
  .nav a:hover { background: #21262d; color: #e6edf3; }
  .nav a.active { background: #21262d; color: #58a6ff; }
  .topbar-right { display: none; }
  .content { padding: 12px; max-width: 1600px; margin: 0 auto; }
  .row { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 10px; margin-bottom: 16px; }
  .row > * { min-width: 0; }
  @media (max-width: 400px) { .row { grid-template-columns: 1fr 1fr; gap: 8px; } }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 12px; position: relative; }
  .card-header { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
  .coin-badge { width: 32px; height: 32px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 13px; flex-shrink: 0; }
  .coin-name { font-size: 16px; font-weight: 700; }
  .price { font-size: 18px; font-weight: 700; color: #58a6ff; }
  .stat-row { display: flex; justify-content: space-between; margin: 4px 0; font-size: 12px; align-items: center; }
  .stat-label { color: #8b949e; }
  .stat-value { font-weight: 600; }
  .green { color: #3fb950; } .red { color: #f85149; } .yellow { color: #d29922; } .muted { color: #8b949e; }
  .section-title { font-size: 12px; font-weight: 700; color: #8b949e; margin: 20px 0 8px; text-transform: uppercase; letter-spacing: 1px; }
  .trade-table { width: 100%; border-collapse: collapse; font-size: 11px; }
  .trade-table th { text-align: left; padding: 6px 8px; color: #8b949e; border-bottom: 1px solid #30363d; font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; }
  .trade-table td { padding: 5px 8px; border-bottom: 1px solid #21262d; }
  .data-table { width: 100%; border-collapse: collapse; font-size: 11px; }
  .data-table th { text-align: left; padding: 8px 10px; color: #8b949e; border-bottom: 1px solid #30363d; font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; white-space: nowrap; }
  .data-table td { padding: 8px 10px; border-bottom: 1px solid #21262d; white-space: nowrap; }
  .table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .trade-table, .data-table { display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .badge { display: inline-block; padding: 2px 6px; border-radius: 10px; font-size: 10px; font-weight: 600; }
  .badge-win  { background: #1a2f1a; color: #3fb950; border: 1px solid #238636; }
  .badge-loss { background: #2d1515; color: #f85149; border: 1px solid #da3633; }
  .badge-open { background: #162032; color: #58a6ff; border: 1px solid #1f6feb; }
  .badge-pass { background: #1c1c1c; color: #8b949e; border: 1px solid #444; }
  .badge-ok   { background: #1a2f1a; color: #3fb950; border: 1px solid #238636; }
  .badge-warn { background: #2d2208; color: #d29922; border: 1px solid #9e6a03; }
  .badge-err  { background: #2d1515; color: #f85149; border: 1px solid #da3633; }
  .mkt-ticker { color: #8b949e; font-size: 10px; font-family: monospace; }
  .alert { padding: 10px 14px; border-radius: 8px; margin-bottom: 10px; font-size: 13px; }
  .alert-ok  { background: #1a2f1a; color: #3fb950; border: 1px solid #238636; }
  .alert-err { background: #2d1515; color: #f85149; border: 1px solid #da3633; }
  .window-bar { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 8px 12px; margin-bottom: 12px; display: flex; gap: 12px; align-items: center; font-size: 12px; flex-wrap: wrap; }
  .btn { background: #21262d; border: 1px solid #30363d; color: #e6edf3; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; touch-action: manipulation; }
  .btn:hover { background: #2d333b; }
  .btn-primary { background: #1f6feb; border-color: #1f6feb; color: #fff; }
  .btn-primary:hover { background: #388bfd; }
  .btn-danger { background: #da3633; border-color: #da3633; color: #fff; }
  .btn-danger:hover { background: #f85149; }
  .form-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 10px 0; }
  .form-label { font-size: 13px; color: #8b949e; min-width: 140px; }
  .form-val { font-size: 13px; font-weight: 600; }
  input[type=text], input[type=number], input[type=password], select, textarea {
    background: #0d1117; border: 1px solid #30363d; color: #e6edf3;
    padding: 8px 10px; border-radius: 6px; font-size: 16px; outline: none;
  }
  input:focus, select:focus, textarea:focus { border-color: #58a6ff; }
  .health-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .dot-ok { background: #3fb950; } .dot-warn { background: #d29922; } .dot-err { background: #f85149; }
  .page-header { font-size: 18px; font-weight: 700; margin-bottom: 4px; }
  .page-sub { font-size: 12px; color: #8b949e; margin-bottom: 16px; }
  .kill-banner { background: #2d1515; border: 1px solid #da3633; color: #f85149; padding: 10px 14px; border-radius: 8px; margin-bottom: 14px; font-weight: 600; font-size: 13px; }
  /* Chat popup */
  #chat-btn { position: fixed; bottom: 20px; right: 16px; width: 48px; height: 48px; border-radius: 50%; background: #1f6feb; border: none; color: #fff; font-size: 20px; cursor: pointer; z-index: 1000; box-shadow: 0 4px 16px rgba(31,111,235,0.4); transition: transform 0.2s; touch-action: manipulation; }
  #chat-btn:hover { transform: scale(1.1); }
  #chat-panel { display: none; position: fixed; bottom: 80px; right: 8px; left: 8px; max-height: 70vh; background: #161b22; border: 1px solid #30363d; border-radius: 12px; z-index: 1000; flex-direction: column; box-shadow: 0 8px 32px rgba(0,0,0,0.6); }
  #chat-panel.open { display: flex; }
  #chat-header { padding: 12px 16px; border-bottom: 1px solid #30363d; font-weight: 700; font-size: 14px; display: flex; justify-content: space-between; align-items: center; }
  #chat-msgs { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
  .chat-msg { padding: 8px 12px; border-radius: 8px; font-size: 13px; line-height: 1.5; max-width: 90%; }
  .chat-msg.user { background: #1f3a5f; align-self: flex-end; }
  .chat-msg.assistant { background: #21262d; align-self: flex-start; }
  #chat-input-row { padding: 10px; border-top: 1px solid #30363d; display: flex; gap: 8px; }
  #chat-input { flex: 1; resize: none; height: 40px; font-family: inherit; font-size: 16px; }
  #chat-send { padding: 8px 14px; }
  @media (min-width: 768px) {
    .topbar { padding: 0 20px; height: 56px; }
    .logo-img { height: 44px; width: 44px; margin-right: 12px; }
    .logo-text { font-size: 18px; margin-right: 20px; }
    .nav a { padding: 8px 12px; font-size: 13px; }
    .topbar-right { display: flex; margin-left: auto; align-items: center; gap: 10px; font-size: 12px; color: #8b949e; white-space: nowrap; }
    .content { padding: 20px; }
    .row { grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; margin-bottom: 20px; }
    .card { padding: 16px; }
    .coin-badge { width: 36px; height: 36px; font-size: 14px; }
    .coin-name { font-size: 18px; }
    .price { font-size: 22px; }
    .stat-row { font-size: 13px; margin: 5px 0; }
    .data-table th { padding: 10px 24px; font-size: 11px; }
    .data-table td { padding: 10px 24px; }
    .trade-table th { padding: 7px 10px; font-size: 11px; }
    .trade-table td { padding: 6px 10px; }
    .form-label { min-width: 180px; }
    input[type=text], input[type=number], input[type=password], select, textarea { font-size: 13px; padding: 6px 10px; }
    #chat-panel { left: auto; right: 24px; width: 360px; bottom: 88px; max-height: 480px; }
    #chat-input { font-size: 13px; height: 36px; }
    #chat-btn { bottom: 24px; right: 24px; width: 52px; height: 52px; font-size: 22px; }
  }
"""

def page_shell(title, active_nav, body, extra_js="", user=None):
    wts = kalshi_window_ts()
    secs_left = max(0, wts + 900 - int(time.time()))
    now_str   = now_cst().strftime("%H:%M " + tz_label())
    wts_cst   = ts_cst(wts).strftime("%H:%M " + tz_label())
    logo_html = f'<img class="logo-img" src="/logo" alt="">' if LOGO_PATH.exists() else ""

    # Kill switch banner
    rs = get_risk_settings()
    kill_banner = '<div class="kill-banner">⚠ KILL SWITCH ACTIVE — All new trades are blocked</div>' if rs["kill_switch"] else ""

    nav_items = [
        ("/",           "Dashboard"),
        ("/trades",     "Trades"),
        ("/decisions",  "Decisions"),
        ("/insights",   "Insights"),
        ("/markets",    "Markets"),
        ("/engines",    "Engines"),
        ("/replay",     "Replay"),
        ("/import",     "Import"),
        ("/fill-quality", "Fill Quality"),
        ("/runs",       "Runs"),
        ("/research",   "Research"),
        ("/providers",  "Providers"),
        ("/audit",      "Audit"),
        ("/settings",   "Settings"),
        ("/health",     "Health"),
    ]
    nav_html = "".join(
        f'<a href="{href}" class="{"active" if href == active_nav else ""}">{label}</a>'
        for href, label in nav_items
    )
    user_html = f'<span>{user["username"]}</span> <a href="/auth/logout" style="color:#8b949e">logout</a>' if user else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — AUTOBET</title>
<style>{SHARED_CSS}{TOOLTIP_CSS}</style>
</head>
<body>
<div class="topbar">
  {logo_html}
  <span class="logo-text">AUTOBET</span>
  <nav class="nav">{nav_html}</nav>
  <div class="topbar-right">
    <span>{now_str}</span>
    <span>Win: {wts_cst}</span>
    <span style="color:{'#3fb950' if secs_left>300 else '#d29922' if secs_left>60 else '#f85149'}">{secs_left}s</span>
    <button class="btn" onclick="location.reload()">↻</button>
    {user_html}
  </div>
</div>
<div class="content">
{kill_banner}
{body}
</div>

<!-- Full-text popup modal -->
<div id="detail-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:10000;align-items:center;justify-content:center">
  <div style="background:#161b22;border:1px solid #444;border-radius:10px;padding:24px;max-width:560px;width:90%;max-height:80vh;overflow-y:auto;position:relative">
    <button onclick="document.getElementById('detail-modal').style.display='none'" style="position:absolute;top:10px;right:12px;background:none;border:none;color:#8b949e;font-size:18px;cursor:pointer">&#x2715;</button>
    <div id="detail-modal-body" style="font-size:13px;line-height:1.7;color:#e6edf3;white-space:pre-wrap;padding-right:16px"></div>
  </div>
</div>
<script>
function showDetail(text) {{
  document.getElementById('detail-modal-body').textContent = text;
  document.getElementById('detail-modal').style.display = 'flex';
}}
document.addEventListener('keydown', function(e){{ if(e.key==='Escape') document.getElementById('detail-modal').style.display='none'; }});
document.getElementById('detail-modal').addEventListener('click', function(e){{ if(e.target===this) this.style.display='none'; }});
</script>

<!-- Chat popup -->
<button id="chat-btn" onclick="toggleChat()" title="Ask Autobet">💬</button>
<div id="chat-panel">
  <div id="chat-header">
    <span>Autobet Chat</span>
    <div style="display:flex;gap:6px">
      <button class="btn" onclick="clearChat()" style="padding:2px 8px;font-size:11px;color:#8b949e" title="Clear history">clear</button>
      <button class="btn" onclick="toggleChat()" style="padding:2px 8px">✕</button>
    </div>
  </div>
  <div id="chat-msgs"></div>
  <div id="chat-input-row">
    <textarea id="chat-input" class="form-input" placeholder="Ask about trades, decisions, settings…" onkeydown="if(event.key==='Enter'&&!event.shiftKey){{event.preventDefault();sendChat();}}"></textarea>
    <button class="btn btn-primary" id="chat-send" onclick="sendChat()">Send</button>
  </div>
</div>

<script>
var _reloadTimer = setTimeout(() => {{ if (!document.getElementById('chat-panel').classList.contains('open')) location.reload(); else setTimeout(() => location.reload(), 30000); }}, 60000);
{extra_js}
// Chat persistence
var CHAT_KEY = 'autobet_chat_v1';
function chatSave(msgs) {{ try {{ localStorage.setItem(CHAT_KEY, JSON.stringify(msgs)); }} catch(e) {{}} }}
function chatLoad() {{ try {{ return JSON.parse(localStorage.getItem(CHAT_KEY) || '[]'); }} catch(e) {{ return []; }} }}
function toggleChat() {{
  var p = document.getElementById('chat-panel');
  p.classList.toggle('open');
  if (p.classList.contains('open')) {{
    var msgs = document.getElementById('chat-msgs');
    msgs.scrollTop = msgs.scrollHeight;
  }}
}}
// Restore chat history on page load
(function() {{
  var history = chatLoad();
  history.forEach(function(m) {{ appendChat(m.role, m.text, false); }});
}})();
function sendChat() {{
  var inp = document.getElementById('chat-input');
  var msg = inp.value.trim();
  if (!msg) return;
  inp.value = '';
  appendChat('user', msg);
  var sendBtn = document.getElementById('chat-send');
  sendBtn.disabled = true;
  sendBtn.textContent = '…';
  var typing = document.createElement('div');
  typing.className = 'chat-msg assistant';
  typing.id = 'chat-typing';
  typing.innerHTML = '<span style="letter-spacing:2px">&#8226;&#8226;&#8226;</span>';
  typing.style.opacity = '0.5';
  document.getElementById('chat-msgs').appendChild(typing);
  document.getElementById('chat-msgs').scrollTop = 99999;
  fetch('/api/chat', {{
    method: 'POST',
    credentials: 'same-origin',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{message: msg}})
  }}).then(function(r) {{
    if (r.status === 401) throw new Error('Session expired — reload the page to log in again');
    return r.json();
  }}).then(function(d) {{
    var t = document.getElementById('chat-typing');
    if (t) t.remove();
    appendChat('assistant', d.reply || d.error || 'No response');
  }}).catch(function(e) {{
    var t = document.getElementById('chat-typing');
    if (t) t.remove();
    appendChat('assistant', 'Error: ' + e.message);
  }}).finally(function() {{
    sendBtn.disabled = false;
    sendBtn.textContent = 'Send';
  }});
}}
function appendChat(role, text, save) {{
  if (save === undefined) save = true;
  var msgs = document.getElementById('chat-msgs');
  var div = document.createElement('div');
  div.className = 'chat-msg ' + role;
  div.textContent = text;
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
  if (save) {{
    var history = chatLoad();
    history.push({{role: role, text: text}});
    if (history.length > 40) history = history.slice(-40);
    chatSave(history);
  }}
}}
function clearChat() {{
  localStorage.removeItem(CHAT_KEY);
  document.getElementById('chat-msgs').innerHTML = '';
}}
</script>
</body>
</html>"""

# ── Login page ──────────────────────────────────────────────────────────────────
def build_login_page(error=""):
    err_html = f'<div style="background:#2d1515;border:1px solid #da3633;color:#f85149;padding:10px 14px;border-radius:6px;margin-bottom:16px;font-size:13px">{error}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Login — AUTOBET</title>
<style>
{SHARED_CSS}
body {{ display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
.login-box {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 40px; width: 360px; }}
.login-title {{ font-size: 24px; font-weight: 800; color: #58a6ff; letter-spacing: 2px; text-align: center; margin-bottom: 8px; }}
.login-sub {{ font-size: 13px; color: #8b949e; text-align: center; margin-bottom: 28px; }}
.field {{ margin-bottom: 16px; }}
.field label {{ display: block; font-size: 13px; color: #8b949e; margin-bottom: 6px; }}
.field input {{ width: 100%; }}
</style>
</head>
<body>
<div class="login-box">
  <div class="login-title">AUTOBET</div>
  <div class="login-sub">Prediction Market Platform</div>
  {err_html}
  <form method="POST" action="/auth/login">
    <div class="field"><label>Username</label><input type="text" name="username" autofocus></div>
    <div class="field"><label>Password</label><input type="password" name="password"></div>
    <button class="btn btn-primary" type="submit" style="width:100%;margin-top:8px;padding:10px">Sign In</button>
  </form>
</div>
</body>
</html>"""

# ── Onboarding wizard ───────────────────────────────────────────────────────────
def build_onboarding_page(step=1, error="", msg=""):
    err_html = f'<div style="background:#2d1515;border:1px solid #da3633;color:#f85149;padding:10px 14px;border-radius:6px;margin-bottom:16px;font-size:13px">{error}</div>' if error else ""
    msg_html = f'<div style="background:#1a2f1a;border:1px solid #238636;color:#3fb950;padding:10px 14px;border-radius:6px;margin-bottom:16px;font-size:13px">{msg}</div>' if msg else ""

    steps_html = ""
    step_labels = ["Admin User", "API Keys", "Paper Config", "Review", "Done"]
    for i, label in enumerate(step_labels, 1):
        active = "color:#58a6ff;font-weight:700" if i == step else "color:#8b949e"
        bullet = "●" if i == step else ("✓" if i < step else "○")
        steps_html += f'<span style="{active};margin-right:16px">{bullet} {label}</span>'

    if step == 1:
        content = f"""
<h2 style="font-size:18px;margin-bottom:8px">Create Admin User</h2>
<p class="page-sub">Set up the admin account to access Autobet.</p>
{err_html}{msg_html}
<form method="POST" action="/onboarding/step/1">
  <div class="form-row"><span class="form-label">Username</span><input type="text" name="username" value="admin" style="width:200px"></div>
  <div class="form-row"><span class="form-label">Password</span><input type="password" name="password" style="width:200px"></div>
  <div class="form-row"><span class="form-label">Confirm Password</span><input type="password" name="confirm" style="width:200px"></div>
  <div style="margin-top:20px"><button class="btn btn-primary" type="submit">Next →</button></div>
</form>"""

    elif step == 2:
        mini_key = MINIMAX_KEY[:20] + "…" if MINIMAX_KEY else ""
        kalshi_id = KALSHI_KEY_ID[:20] + "…" if KALSHI_KEY_ID else ""
        pem_ok = KALSHI_PEM.exists()
        content = f"""
<h2 style="font-size:18px;margin-bottom:8px">API Keys</h2>
<p class="page-sub">Credentials are loaded from <code>~/autoresearch/.env</code>. Current status:</p>
{err_html}{msg_html}
<div class="card" style="margin-bottom:16px">
  <div class="stat-row"><span class="stat-label"><span class="health-dot {'dot-ok' if MINIMAX_KEY else 'dot-err'}"></span>MiniMax API Key</span><span class="stat-value">{"✓ " + mini_key if MINIMAX_KEY else "✗ Missing"}</span></div>
  <div class="stat-row"><span class="stat-label"><span class="health-dot {'dot-ok' if KALSHI_KEY_ID else 'dot-err'}"></span>Kalshi Key ID</span><span class="stat-value">{"✓ " + kalshi_id if KALSHI_KEY_ID else "✗ Missing"}</span></div>
  <div class="stat-row"><span class="stat-label"><span class="health-dot {'dot-ok' if pem_ok else 'dot-err'}"></span>Kalshi PEM File</span><span class="stat-value">{"✓ Found" if pem_ok else "✗ Missing at " + str(KALSHI_PEM)}</span></div>
</div>
<p style="font-size:13px;color:#8b949e;margin-bottom:16px">Edit <code>~/autoresearch/.env</code> and restart if keys are missing. You can continue without all keys.</p>
<form method="POST" action="/onboarding/step/2">
  <button class="btn btn-primary" type="submit">Next →</button>
  <a href="/onboarding?step=1"><button class="btn" type="button" style="margin-left:8px">← Back</button></a>
</form>"""

    elif step == 3:
        content = f"""
<h2 style="font-size:18px;margin-bottom:8px">Paper Trading Defaults</h2>
<p class="page-sub">Set starting capital and trade size for paper experiments.</p>
{err_html}{msg_html}
<form method="POST" action="/onboarding/step/3">
  <div class="form-row"><span class="form-label">Starting Capital / coin ($)</span><input type="number" name="starting_capital" value="500" step="50" style="width:120px"></div>
  <div class="form-row"><span class="form-label">Trade Size ($)</span><input type="number" name="trade_size" value="20" step="1" style="width:120px"></div>
  <div class="form-row"><span class="form-label">Daily Loss Limit ($)</span><input type="number" name="daily_loss_limit" value="100" step="1" style="width:120px"></div>
  <div class="form-row"><span class="form-label">Max Drawdown (%)</span><input type="number" name="max_drawdown_pct" value="30" step="1" style="width:120px"></div>
  <div style="margin-top:20px">
    <button class="btn btn-primary" type="submit">Next →</button>
    <a href="/onboarding?step=2"><button class="btn" type="button" style="margin-left:8px">← Back</button></a>
  </div>
</form>"""

    elif step == 4:
        content = f"""
<h2 style="font-size:18px;margin-bottom:8px">Review &amp; Safety Confirmation</h2>
<p class="page-sub">Confirm your setup before starting.</p>
{err_html}{msg_html}
<div class="card" style="margin-bottom:16px">
  <div class="stat-row"><span class="stat-label">Coins tracked</span><span class="stat-value">{', '.join(COINS)}</span></div>
  <div class="stat-row"><span class="stat-label">Default mode</span><span class="stat-value"><span class="badge badge-open">PAPER</span></span></div>
  <div class="stat-row"><span class="stat-label">Live trading</span><span class="stat-value"><span class="badge badge-err">OFF (global toggle)</span></span></div>
  <div class="stat-row"><span class="stat-label">Venues</span><span class="stat-value">Kalshi + Polymarket (observe)</span></div>
</div>
<div style="background:#162032;border:1px solid #1f6feb;border-radius:8px;padding:12px 16px;margin-bottom:16px;font-size:13px;color:#8b949e">
  <strong style="color:#58a6ff">Safety note:</strong> Live trading is OFF by default. The global live toggle and per-coin mode must both be enabled before real orders are placed. All trades are paper-only until you explicitly enable live mode.
</div>
<form method="POST" action="/onboarding/step/4">
  <button class="btn btn-primary" type="submit">Finish Setup →</button>
  <a href="/onboarding?step=3"><button class="btn" type="button" style="margin-left:8px">← Back</button></a>
</form>"""

    else:  # step 5 — done
        content = f"""
<h2 style="font-size:18px;margin-bottom:8px;color:#3fb950">✓ Setup Complete</h2>
<p class="page-sub">Autobet is ready. Data collection is running in the background.</p>
{msg_html}
<a href="/"><button class="btn btn-primary" style="margin-top:8px">Go to Dashboard →</button></a>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Setup — AUTOBET</title>
<style>{SHARED_CSS}</style>
</head>
<body>
<div style="max-width:600px;margin:60px auto;padding:0 20px">
  <div style="font-size:22px;font-weight:800;color:#58a6ff;letter-spacing:2px;margin-bottom:24px">AUTOBET <span style="font-size:14px;color:#8b949e;font-weight:400;letter-spacing:0">Setup Wizard</span></div>
  <div style="margin-bottom:24px;font-size:13px">{steps_html}</div>
  <div class="card">{content}</div>
</div>
</body>
</html>"""


# ── Dashboard ──────────────────────────────────────────────────────────────────
def build_dashboard(user=None):
    conn = db_connect()
    accts = {r[0]: r for r in conn.execute(
        "SELECT coin, capital, wins, losses, total_pnl FROM paper_accounts"
    ).fetchall()}
    recent_trades = {}
    for coin in COINS:
        rows = conn.execute("""
            SELECT window_ts, direction, entry, size, pnl, result, coin_open, coin_close
            FROM paper_trades WHERE coin=? AND result IS NOT NULL
            ORDER BY window_ts DESC LIMIT 5
        """, (coin,)).fetchall()
        recent_trades[coin] = rows
    open_trades = {}
    for coin in COINS:
        row = conn.execute("""
            SELECT direction, entry, size, decided_at FROM paper_trades
            WHERE coin=? AND result IS NULL ORDER BY window_ts DESC LIMIT 1
        """, (coin,)).fetchone()
        open_trades[coin] = row
    # Per-coin EV (win_rate * avg_win_profit - loss_rate * avg_loss, fee-adjusted)
    coin_ev = {}
    for coin in COINS:
        ev_rows = conn.execute("""
            SELECT result, entry, pnl FROM paper_trades
            WHERE coin=? AND result IN ('WIN','LOSS') AND entry > 0 AND pnl IS NOT NULL
            ORDER BY window_ts DESC LIMIT 30
        """, (coin,)).fetchall()
        if ev_rows:
            wins_ev   = [r for r in ev_rows if r[0]=='WIN']
            losses_ev = [r for r in ev_rows if r[0]=='LOSS']
            wr = len(wins_ev)/len(ev_rows)
            avg_win  = sum(r[2] for r in wins_ev)/len(wins_ev) if wins_ev else 0
            avg_loss = sum(r[2] for r in losses_ev)/len(losses_ev) if losses_ev else 0
            ev = wr * avg_win + (1-wr) * avg_loss
            coin_ev[coin] = (ev, len(ev_rows), wr)
    # Ex-outlier P&L: total minus top 3 wins (shows real performance without lucky strikes)
    coin_ex_outlier = {}
    for coin in COINS:
        all_pnl = conn.execute("""
            SELECT pnl FROM paper_trades WHERE coin=? AND result IN ('WIN','LOSS')
            ORDER BY pnl DESC
        """, (coin,)).fetchall()
        if all_pnl:
            total = sum(r[0] or 0 for r in all_pnl)
            top3  = sum(r[0] or 0 for r in all_pnl[:3])
            coin_ex_outlier[coin] = (total, total - top3, top3)
    # Live toggle state
    state = conn.execute("SELECT global_live_enabled FROM system_state WHERE id=1").fetchone()
    live_on = state and state[0] == 1
    # Pool mode
    pool_row = conn.execute("SELECT value FROM settings WHERE key='pool_mode'").fetchone()
    pool_on = pool_row and pool_row[0] == "1"
    pool_bal_row = conn.execute("SELECT value FROM settings WHERE key='pool_balance'").fetchone()
    pool_balance = float(pool_bal_row[0]) if pool_bal_row and pool_bal_row[0] else None
    # Per-coin live mode and last live order
    coin_modes_db = {r[0]: r[1] for r in conn.execute("SELECT coin, mode FROM coin_modes").fetchall()}
    coin_last_order = {}
    recent_cutoff = int(time.time()) - 1800  # only show orders from last 30 min
    for coin in COINS:
        row = conn.execute(
            "SELECT direction, contracts, status, error, created_at, window_ts, avg_fill_price, filled_contracts FROM live_orders WHERE coin=? AND window_ts>=? ORDER BY id DESC LIMIT 1",
            (coin, recent_cutoff)
        ).fetchone()
        coin_last_order[coin] = row
    # Live order actual W/L per coin (filled + resolved, not paper)
    live_wl = {}
    for coin in COINS:
        r = conn.execute("""
            SELECT
              SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
              SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END),
              COALESCE(SUM(pnl), 0)
            FROM live_orders
            WHERE coin=? AND status IN ('filled','executed','filled_partial')
              AND resolved_at IS NOT NULL
        """, (coin,)).fetchone()
        if r and (r[0] or r[1]):
            live_wl[coin] = (int(r[0] or 0), int(r[1] or 0), float(r[2] or 0))
    # Recent live order history per coin (activity log style, last 5)
    coin_live_history = {}
    for coin in COINS:
        rows = conn.execute("""
            SELECT direction, contracts, avg_fill_price, status, pnl, window_ts
            FROM live_orders WHERE coin=?
            ORDER BY id DESC LIMIT 5
        """, (coin,)).fetchall()
        coin_live_history[coin] = rows
    # Pool: last winner this session
    pool_last = conn.execute(
        "SELECT coin, direction, contracts, status, window_ts FROM live_orders ORDER BY id DESC LIMIT 1"
    ).fetchone()
    pool_recent_orders = conn.execute(
        "SELECT coin, direction, contracts, status, window_ts, limit_price FROM live_orders WHERE window_ts >= ? ORDER BY id DESC LIMIT 10",
        (int(time.time()) - 7200,)
    ).fetchall()
    conn.close()

    with _state_lock:
        prices = dict(_prices)
        mkts   = {k: dict(v) for k, v in _active_mkts.items()}
        poly   = dict(_poly_mkts)

    wts = kalshi_window_ts()
    secs_left = max(0, wts + 900 - int(time.time()))
    wts_cst_str = ts_cst(wts).strftime("%H:%M " + tz_label())

    mode_badge = f'<span class="badge badge-ok">LIVE</span>' if live_on else f'<span class="badge badge-open">PAPER</span>'

    body = f"""
<div class="window-bar">
  <span>Window: <strong>{wts_cst_str}</strong></span>
  <span>Remaining: <strong style="color:{'#3fb950' if secs_left>300 else '#d29922' if secs_left>60 else '#f85149'}">{secs_left}s</strong></span>
  <span>Mode: {mode_badge}</span>
  <span>Model: <strong>{get_minimax_model()}</strong></span>
</div>
<div class="section-title">Market Snapshot</div>
<div class="row">
"""
    for coin in COINS:
        color  = COIN_COLORS[coin]
        letter = COIN_LETTERS[coin]
        price  = prices.get(coin)
        price_str = (f"${price:,.4f}" if price and price < 10 else f"${price:,.2f}") if price else "---"
        acct   = accts.get(coin)
        capital    = acct[1] if acct else STARTING_CAPITAL
        coin_is_live_mode = live_on and coin_modes_db.get(coin) == "live"
        lw = live_wl.get(coin) if coin_is_live_mode else None
        if lw:
            # Live mode: show actual filled+settled order outcomes
            wins, losses, total_pnl = lw
        else:
            wins      = acct[2] if acct else 0
            losses    = acct[3] if acct else 0
            total_pnl = acct[4] if acct else 0
        total_trades = wins + losses
        win_rate   = f"{wins/total_trades*100:.0f}%" if total_trades > 0 else "—"
        pnl_cls    = "green" if total_pnl >= 0 else "red"
        mkt        = mkts.get(coin, {})
        yes_bid    = mkt.get("yes_bid", 0)
        yes_ask    = mkt.get("yes_ask", 0)
        ticker     = mkt.get("ticker", "—")
        open_t     = open_trades.get(coin)
        poly_m     = poly.get(coin, {})
        poly_price = poly_m.get("yes_price")
        poly_str   = f'{poly_price:.3f}' if poly_price else "—"
        if open_t:
            d2, e2, sz2 = open_t[0], open_t[1], open_t[2]
            open_badge = f'<a href="/coin/{coin}" style="text-decoration:none"><span class="badge badge-open" title="Active paper bet — click for details">{d2} @ {e2:.3f} &nbsp;${sz2:.0f} stake</span></a>'
        else:
            open_badge = '<span class="muted" style="font-size:12px">no open bet</span>'
        ev_data = coin_ev.get(coin)
        if ev_data:
            ev_val, ev_n, ev_wr = ev_data
            ev_cls = "green" if ev_val > 0.02 else "red" if ev_val < -0.02 else "yellow"
            ev_bar_w = min(abs(ev_val) * 400, 100)
            ev_bar_col = "#238636" if ev_val > 0 else "#da3633"
            ev_html = f'<div style="margin:5px 0 2px 0"><div style="display:flex;align-items:center;gap:6px"><span style="font-size:10px;color:#8b949e">Edge</span><div style="flex:1;height:4px;background:#21262d;border-radius:2px"><div style="width:{ev_bar_w:.0f}%;height:100%;background:{ev_bar_col};border-radius:2px"></div></div><span style="font-size:10px;font-weight:700" class="{ev_cls}">EV {ev_val:+.3f}</span>{tooltip_html("edge")}</div></div>'
        else:
            ev_html = '<div style="margin:5px 0 2px 0;font-size:10px;color:#555">Edge — no data yet</div>'
        # Live activity log (Kalshi-style)
        last_order = coin_last_order.get(coin)
        if coin_is_live_mode:
            hist = coin_live_history.get(coin, [])
            # Check for active position this window
            active_html = ""
            if last_order:
                lo_dir, lo_contracts, lo_status, lo_error, lo_time, lo_wts, lo_avg_price, lo_filled = last_order
                is_active = (lo_wts == wts and lo_status in ("filled", "executed", "filled_partial")
                             and lo_filled and lo_avg_price)
                if is_active and yes_bid and yes_ask:
                    n = int(lo_filled or 0)
                    avg_p = float(lo_avg_price or 0)
                    if lo_dir == "NO":
                        cur_val = 1.0 - yes_bid
                        unreal  = n * (cur_val - avg_p)
                    else:
                        cur_val = yes_bid
                        unreal  = n * (cur_val - avg_p)
                    unreal_color = "#3fb950" if unreal >= 0 else "#f85149"
                    unreal_s = f'+${unreal:.2f}' if unreal >= 0 else f'-${abs(unreal):.2f}'
                    elapsed_pct = min(100, max(0, (900 - secs_left) / 900 * 100))
                    bar_color = "#3fb950" if unreal >= 0 else "#f85149"
                    entry_s = f"{avg_p*100:.0f}¢"
                    cur_s   = f"{cur_val*100:.0f}¢"
                    active_html = f'''<div style="margin-bottom:4px;padding:4px 0;border-bottom:1px solid #21262d">
  <div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">
    <span style="font-size:9px;background:{unreal_color};color:#000;padding:1px 5px;border-radius:3px;font-weight:700">LIVE</span>
    <span style="font-size:11px;color:#e6edf3;font-weight:700">{lo_dir}</span>
    <span style="font-size:10px;color:#8b949e">{n}c @ {entry_s}</span>
    <span style="font-size:11px;color:#8b949e">→</span>
    <span style="font-size:11px;color:{unreal_color};font-weight:700">{cur_s}</span>
    <span style="font-size:11px;color:{unreal_color};font-weight:700;margin-left:auto">{unreal_s}</span>
  </div>
  <div style="height:2px;background:#21262d;border-radius:2px">
    <div style="width:{elapsed_pct:.0f}%;height:100%;background:{bar_color};border-radius:2px"></div>
  </div>
  <div style="display:flex;justify-content:space-between;margin-top:2px">
    <span style="font-size:9px;color:#555">{900-secs_left:.0f}s elapsed</span>
    <span style="font-size:9px;color:#555">{secs_left}s left</span>
  </div>
</div>'''
            # Activity log rows
            rows_html = ""
            for h_dir, h_contracts, h_avg_price, h_status, h_pnl, h_wts in hist:
                h_time_s = ts_cst(h_wts).strftime("%H:%M") if h_wts else "—"
                if h_status == "canceled":
                    row_color = "#8b949e"
                    result_s  = "canceled"
                    pnl_s     = ""
                elif h_pnl is not None and h_status in ("filled", "executed", "filled_partial"):
                    if h_pnl > 0:
                        row_color = "#3fb950"
                        result_s  = "WIN"
                        pnl_s     = f'<span style="color:#3fb950;font-weight:700;margin-left:auto;flex-shrink:0;font-size:10px">+${h_pnl:.2f}</span>'
                    else:
                        row_color = "#f85149"
                        result_s  = "LOSS"
                        pnl_s     = f'<span style="color:#f85149;font-weight:700;margin-left:auto;flex-shrink:0;font-size:10px">-${abs(h_pnl):.2f}</span>'
                else:
                    row_color = "#e3b341"
                    result_s  = h_status
                    pnl_s     = ""
                entry_disp = f"{float(h_avg_price)*100:.0f}¢" if h_avg_price else "—"
                rows_html += f'<div style="display:flex;align-items:center;gap:6px;padding:2px 0;border-bottom:1px solid #21262d;min-width:0">' \
                             f'<span style="font-size:9px;color:#6e7681;min-width:30px;flex-shrink:0">{h_time_s}</span>' \
                             f'<span style="font-size:10px;color:#e6edf3;font-weight:600;min-width:22px;flex-shrink:0">{h_dir}</span>' \
                             f'<span style="font-size:10px;color:#8b949e;flex-shrink:0">{h_contracts}c @ {entry_disp}</span>' \
                             f'<span style="font-size:10px;color:{row_color};margin-left:4px;flex-shrink:0">{result_s}</span>' \
                             f'{pnl_s}</div>'
            if rows_html or active_html:
                live_indicator = f'<div style="margin-top:6px;padding:6px 0 4px 0;border-top:1px solid #21262d;overflow:hidden;box-sizing:border-box;width:100%" onclick="event.stopPropagation();window.location=\'/fill-quality\'">' \
                                 f'<div style="font-size:9px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">LIVE ORDERS</div>' \
                                 f'{active_html}{rows_html}</div>'
            else:
                live_indicator = f'<div style="margin-top:6px;padding:4px 8px;border-radius:6px;background:#0d1117;border:1px solid #30363d;display:flex;align-items:center;gap:6px">' \
                                 f'<span style="font-size:9px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px">LIVE</span>' \
                                 f'<span style="font-size:11px;color:#555">no orders yet</span></div>'
        else:
            live_indicator = ""

        body += f"""<div class="card" style="cursor:pointer;transition:border-color 0.15s" onclick="window.location='/coin/{coin}'" onmouseover="this.style.borderColor='{color}'" onmouseout="this.style.borderColor=''">
  <div class="card-header">
    <div class="coin-badge" style="background:{color}">{letter}</div>
    <div><div class="coin-name">{coin}</div><div class="mkt-ticker" style="font-size:10px">{ticker}</div></div>
    <div style="margin-left:auto;text-align:right"><div class="price" style="font-size:15px;color:{'#3fb950' if total_pnl>=0 else '#f85149'}">${(pool_balance if (pool_on and coin_is_live_mode and pool_balance is not None) else capital):.2f}</div><div style="font-size:10px;color:#8b949e">{'pool' if (pool_on and coin_is_live_mode and pool_balance is not None) else price_str}</div></div>
  </div>
  <div class="stat-row"><span class="stat-label">Kalshi Bid/Ask</span><span class="stat-value">{yes_bid:.3f} / {yes_ask:.3f}</span></div>
  <div style="margin:4px 0 2px 0">{prob_bar(yes_bid, yes_ask, poly_price)}<span style="font-size:10px;color:#8b949e;margin-left:4px">{tooltip_html("prob_bar")}</span></div>
  {ev_html}
  <div class="stat-row"><span class="stat-label">P&amp;L / Rate</span><span class="stat-value"><span class="{pnl_cls}">${total_pnl:+.2f}</span> &nbsp; {win_rate}</span></div>
  <div class="stat-row" style="font-size:10px"><span class="stat-label" style="color:#555">ex-outlier P&amp;L {tooltip_html("ex_outlier")}</span><span class="stat-value" style="font-size:10px"><span class="{'green' if (coin_ex_outlier.get(coin,(0,0,0))[1])>=0 else 'red'}">${coin_ex_outlier.get(coin,(0,0,0))[1]:+.2f}</span></span></div>
  <div style="margin-top:8px" onclick="event.stopPropagation()">{open_badge}</div>
  {live_indicator}
</div>
"""
    body += '</div>\n'

    # ── Pool mode balance card ────────────────────────────────────────────────────
    if pool_on and live_on:
        live_coins_pool = [c for c in COINS if coin_modes_db.get(c) == "live"]
        bal_s = f"${pool_balance:,.2f}" if pool_balance is not None else "syncing…"
        coins_s = " · ".join(f'<span style="color:{COIN_COLORS[c]};font-weight:700">{c}</span>' for c in live_coins_pool) or "none"
        orders_html = ""
        if pool_recent_orders:
            orders_html = '<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:8px">'
            for oc, od, ocontracts, ostatus, owts, olimit_price in pool_recent_orders:
                sc = {"filled": "#3fb950", "placed": "#e3b341", "failed": "#f85149", "canceled": "#8b949e", "executed": "#3fb950"}.get(ostatus, "#8b949e")
                proj = ""
                try:
                    if ostatus not in ("canceled", "failed") and olimit_price and ocontracts:
                        ep = float(olimit_price) / 100.0
                        gross = ocontracts * (1.0 - ep)
                        fee = min(KALSHI_FEE_RATE * ep * (1.0 - ep), 0.02) * ocontracts
                        net = gross - fee
                        proj = f' <span style="color:#3fb950;font-weight:700">+${net:.2f}</span>'
                except Exception:
                    pass
                orders_html += f'<span style="background:#0d1117;border:1px solid {sc};border-radius:6px;padding:3px 8px;font-size:12px">'
                orders_html += f'<span style="color:{COIN_COLORS.get(oc,"#ccc")};font-weight:700">{oc}</span> '
                orders_html += f'<span style="color:#e6edf3">{od}</span> '
                orders_html += f'<span style="color:#8b949e">{ocontracts}c</span>'
                orders_html += proj
                orders_html += '</span>'
            orders_html += '</div>'
        body += f'''<div class="card" style="margin-bottom:16px;border-color:#1f6feb">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:6px">
    <div style="width:44px;height:44px;border-radius:8px;background:#1f6feb;display:flex;align-items:center;justify-content:center;font-size:18px;font-weight:800;color:#fff">P</div>
    <div>
      <div style="font-size:11px;color:#8b949e;font-weight:600;text-transform:uppercase;letter-spacing:1px">Pool Mode</div>
      <div style="font-size:22px;font-weight:800;color:#e6edf3">{bal_s}</div>
    </div>
    <div style="margin-left:auto;text-align:right">
      <div style="font-size:11px;color:#8b949e">competing coins</div>
      <div style="font-size:13px;margin-top:2px">{coins_s}</div>
    </div>
  </div>
  <div style="font-size:11px;color:#8b949e">One live trade per window · winner picked by confidence + win rate</div>
  {orders_html}
</div>\n'''

    body += '<div class="section-title">Recent Trades</div>\n'
    for coin in COINS:
        trades = recent_trades.get(coin, [])
        color  = COIN_COLORS[coin]
        letter = COIN_LETTERS[coin]
        body += f'<div class="card" style="margin-bottom:12px">\n'
        body += f'<div class="card-header"><div class="coin-badge" style="background:{color}">{letter}</div><div class="coin-name">{coin}</div></div>\n'
        if not trades:
            body += '<div class="muted" style="font-size:13px;padding:4px 0">No completed trades yet</div>\n'
        else:
            body += '<table class="trade-table"><tr><th>Time (CT)</th><th>Dir</th><th>Entry</th><th>Open→Close</th><th>P&amp;L</th><th>Result</th></tr>\n'
            for t in trades:
                wts2, direction, entry, size, pnl, result, coin_open, coin_close = t
                t_str = ts_cst(wts2).strftime("%m/%d %H:%M")
                badge_cls = "badge-win" if result == "WIN" else "badge-loss"
                pnl_cls2  = "green" if (pnl or 0) >= 0 else "red"
                co = f"${coin_open:,.2f}" if coin_open else "?"
                cc = f"${coin_close:,.2f}" if coin_close else "?"
                pnl_s = f"${pnl:+.2f}" if pnl is not None else "?"
                body += f'<tr><td>{t_str}</td><td>{direction}</td><td>{entry:.3f}</td><td>{co}→{cc}</td>'
                body += f'<td class="{pnl_cls2}">{pnl_s}</td><td><span class="badge {badge_cls}">{result}</span></td></tr>\n'
            body += '</table>\n'
        body += '</div>\n'
    return page_shell("Dashboard", "/", body, user=user)


# ── Trades page ─────────────────────────────────────────────────────────────────
def build_trades_page(user=None):
    conn = db_connect()
    rows = conn.execute("""
        SELECT coin, window_ts, direction, actual, entry, size, pnl, fee, result,
               coin_open, coin_close, decided_at, resolved_at
        FROM paper_trades ORDER BY window_ts DESC LIMIT 200
    """).fetchall()
    conn.close()

    body = '<div class="page-header">Paper Trades</div>\n'
    body += '<div class="page-sub">All paper trade history, most recent first.</div>\n'
    if not rows:
        body += '<div class="card"><div class="muted">No trades yet — decisions fire in the first 3 minutes of each 15-min window.</div></div>'
    else:
        body += '<div class="card">\n'
        body += '<table class="trade-table"><tr><th>Time (CT)</th><th>Coin</th><th>Dir</th><th>Actual</th><th>Entry</th><th>Size</th><th>Open</th><th>Close</th><th>P&amp;L</th><th>Fee</th><th>Result</th></tr>\n'
        for r in rows:
            coin, wts, direction, actual, entry, size, pnl, fee, result, co, cc, decided_at, resolved_at = r
            color = COIN_COLORS.get(coin, "#555")
            t_str = ts_cst(wts).strftime("%m/%d %H:%M") if wts else "?"
            actual_s = actual or "—"
            co_s  = f"${co:,.2f}" if co else "?"
            cc_s  = f"${cc:,.2f}" if cc else "?"
            pnl_s = f"${pnl:+.2f}" if pnl is not None else "open"
            fee_s = f"${fee:.3f}" if fee else "—"
            if result == "WIN":
                badge = '<span class="badge badge-win">WIN</span>'
            elif result == "LOSS":
                badge = '<span class="badge badge-loss">LOSS</span>'
            elif direction == "PASS":
                badge = '<span class="badge badge-pass">PASS</span>'
            else:
                badge = '<span class="badge badge-open">OPEN</span>'
            pnl_cls = "green" if (pnl or 0) > 0 else "red" if (pnl or 0) < 0 else ""
            body += f'<tr><td>{t_str}</td>'
            body += f'<td><span style="color:{color};font-weight:700">{coin}</span></td>'
            body += f'<td>{direction}</td><td>{actual_s}</td><td>{entry:.3f}</td><td>${size:.0f}</td>'
            body += f'<td>{co_s}</td><td>{cc_s}</td>'
            body += f'<td class="{pnl_cls}">{pnl_s}</td><td>{fee_s}</td><td>{badge}</td></tr>\n'
        body += '</table>\n</div>\n'
    return page_shell("Trades", "/trades", body, user=user)


# ── Decisions page ──────────────────────────────────────────────────────────────
def build_decisions_page(user=None):
    conn = db_connect()
    # Coin filter from URL (passed via qs in handler)
    rows = conn.execute("""
        SELECT d.coin, d.window_ts, d.direction, d.entry, d.confidence, d.rationale, d.decided_at,
               pt.result, pt.pnl, pt.size, pt.coin_open, pt.coin_close, pt.actual
        FROM decisions d
        LEFT JOIN paper_trades pt ON pt.coin=d.coin AND pt.window_ts=d.window_ts
        ORDER BY d.window_ts DESC LIMIT 200
    """).fetchall()

    # Also show Kalshi windows with NO decision (passed windows)
    # Get all distinct window_ts from ticks that have no decision
    decided_wts = {(r[0], r[1]) for r in rows}
    all_windows = conn.execute("""
        SELECT DISTINCT coin, window_ts FROM kalshi_ticks
        WHERE ts > ? ORDER BY window_ts DESC LIMIT 200
    """, (int(time.time()) - 7*86400,)).fetchall()
    conn.close()

    body = '<div class="page-header">Decisions</div>\n'
    body += '<div class="page-sub">Every 15-minute window — what the engine decided and why. Windows with no trade show the reason they were skipped.</div>\n'

    # Build combined rows: decisions + undecided windows
    combined = {}
    for r in rows:
        coin, wts = r[0], r[1]
        combined[(coin, wts)] = ("decision", r)
    for coin, wts in all_windows:
        if (coin, wts) not in combined:
            combined[(coin, wts)] = ("skipped", None)

    # Sort by wts desc
    sorted_items = sorted(combined.items(), key=lambda x: x[0][1], reverse=True)[:200]

    if not sorted_items:
        body += '<div class="card"><div class="muted">No windows yet.</div></div>'
    else:
        body += '<div class="card" style="overflow-x:auto">\n'
        body += '<table class="trade-table"><tr><th>Time (CT)</th><th>Coin</th><th>Action</th><th>Entry</th><th>Conf</th><th>Size</th><th>Open→Close</th><th>Outcome</th><th>P&L</th><th>Why</th></tr>\n'
        for (coin, wts), (kind, r) in sorted_items:
            color = COIN_COLORS.get(coin, "#555")
            t_str = ts_cst(wts).strftime("%m/%d %H:%M")
            if kind == "skipped":
                body += f'<tr style="opacity:0.45"><td>{t_str}</td>'
                body += f'<td><span style="color:{color};font-weight:700">{coin}</span></td>'
                body += f'<td><span class="badge badge-pass">NO DATA</span></td>'
                body += f'<td colspan="7" class="muted" style="font-size:11px">No tick data collected for this window</td></tr>\n'
                continue
            coin, wts, direction, entry, confidence, rationale, decided_at, result, pnl, size, coin_open, coin_close, actual = r
            conf_s = f"{confidence:.0%}" if confidence is not None else "—"
            entry_s = f"{entry:.3f}" if entry else "—"
            size_s  = f"${size:.0f}" if size else "—"
            co_s = (f"${coin_open:,.2f}" if coin_open and coin_open > 10 else f"${coin_open:,.4f}" if coin_open else "?")
            cc_s = (f"${coin_close:,.2f}" if coin_close and coin_close > 10 else f"${coin_close:,.4f}" if coin_close else "?")
            oc_str = f"{co_s}→{cc_s}" if coin_open and coin_close else "—"
            rat_full = rationale or ""
            rat_short = rat_full[:70] + ("…" if len(rat_full) > 70 else "")

            if direction == "PASS":
                action = '<span class="badge badge-pass">PASS</span>'
                outcome = '<span class="badge badge-pass">—</span>'
                pnl_s = "—"; pnl_cls = "muted"
            elif result == "WIN":
                action  = f'<span class="badge badge-win">{direction}</span>'
                outcome = '<span class="badge badge-win">WIN</span>'
                pnl_s = f"${pnl:+.2f}"; pnl_cls = "green"
            elif result == "LOSS":
                action  = f'<span class="badge badge-loss">{direction}</span>'
                outcome = '<span class="badge badge-loss">LOSS</span>'
                pnl_s = f"${pnl:+.2f}"; pnl_cls = "red"
            else:
                action  = f'<span class="badge badge-open">{direction}</span>'
                outcome = f'<span class="badge badge-open">{"OPEN" if direction not in ("PASS","") else "—"}</span>'
                pnl_s = "open" if direction not in ("PASS","") else "—"
                pnl_cls = "muted"

            body += f'<tr><td>{t_str}</td>'
            body += f'<td><a href="/coin/{coin}" style="color:{color};font-weight:700;text-decoration:none">{coin}</a></td>'
            body += f'<td>{action}</td><td>{entry_s}</td><td>{conf_s}</td><td>{size_s}</td>'
            body += f'<td style="font-size:11px">{oc_str}</td><td>{outcome}</td>'
            body += f'<td class="{pnl_cls}">{pnl_s}</td>'
            rat_esc = rat_full.replace("\\", "\\\\").replace("'", "\\'")
            clickable = f' onclick="showDetail(\'{rat_esc}\')" style="cursor:pointer"' if len(rat_full) > 70 else ''
            body += f'<td class="muted" style="font-size:11px"{clickable}>{rat_short}</td></tr>\n'
        body += '</table>\n</div>\n'
    return page_shell("Decisions", "/decisions", body, user=user)


# ── Markets page ────────────────────────────────────────────────────────────────
def build_markets_page(user=None, msg=""):
    conn = db_connect()
    settings = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    conn.close()

    with _state_lock:
        mkts  = {k: dict(v) for k, v in _active_mkts.items()}
        poly  = dict(_poly_mkts)
        prices = dict(_prices)

    msg_html = f'<div style="background:#1a2f1a;border:1px solid #238636;color:#3fb950;padding:10px;border-radius:6px;margin-bottom:16px;font-size:13px">{msg}</div>' if msg else ""

    body = f'<div class="page-header">Markets {tooltip_html("market_group")}</div>\n'
    body += '<div class="page-sub">Market group configuration, execution mode, and live venue data.</div>\n'
    body += msg_html

    # Global live toggle + pool mode
    conn2 = db_connect()
    state = conn2.execute("SELECT global_live_enabled FROM system_state WHERE id=1").fetchone()
    pool_row = conn2.execute("SELECT value FROM settings WHERE key='pool_mode'").fetchone()
    conn2.close()
    live_on = state and state[0] == 1
    pool_on = pool_row and pool_row[0] == "1"
    live_badge = '<span class="badge badge-ok">ON</span>' if live_on else '<span class="badge badge-err">OFF</span>'
    live_action = "disable" if live_on else "enable"
    live_label  = "Disable Live" if live_on else "Enable Live"
    live_cls    = "btn-danger" if live_on else "btn-primary"
    body += f"""<div class="card" style="margin-bottom:16px">
  <div class="stat-row">
    <span class="stat-label"><strong>Global Live Trading {tooltip_html("global_live_enabled")}</strong></span>
    <span class="stat-value">{live_badge}
      <form method="POST" action="/markets/live-toggle" style="display:inline;margin-left:12px">
        <button class="btn {live_cls}" type="submit" onclick="return confirm('Toggle global live trading?')">{live_label}</button>
      </form>
    </span>
  </div>
  <div class="stat-row" style="margin-top:10px">
    <span class="stat-label"><strong>Pool Mode</strong> <span style="font-size:11px;color:#8b949e">— one trade per window, best live signal wins</span></span>
    <span class="stat-value">
      {'<span class="badge badge-ok">ON</span>' if pool_on else '<span class="badge badge-pass">OFF</span>'}
      <form method="POST" action="/markets/pool-toggle" style="display:inline;margin-left:12px">
        <button class="btn" type="submit">{'Disable Pool Mode' if pool_on else 'Enable Pool Mode'}</button>
      </form>
    </span>
  </div>
</div>
"""
    body += '<div class="section-title">Market Groups</div>\n'
    body += '<div class="card">\n'
    body += '<table class="trade-table"><tr><th>Coin</th><th>Mode</th><th>Trade Venue</th><th>Kalshi Ticker</th><th>Bid/Ask</th><th>Polymarket</th><th>Price</th><th>Action</th></tr>\n'

    for coin in COINS:
        color  = COIN_COLORS[coin]
        letter = COIN_LETTERS[coin]
        mode   = settings.get(f"mode_{coin}", "paper")
        mkt    = mkts.get(coin, {})
        ticker = mkt.get("ticker", "—")
        yes_bid = mkt.get("yes_bid", 0)
        yes_ask = mkt.get("yes_ask", 0)
        poly_m  = poly.get(coin, {})
        poly_p  = poly_m.get("yes_price")
        poly_s  = f"{poly_p:.3f}" if poly_p else "—"
        price   = prices.get(coin)
        price_s = (f"${price:,.4f}" if price and price < 10 else f"${price:,.2f}") if price else "—"

        mode_opts = ""
        for m in ["observe", "paper", "live"]:
            sel = "selected" if m == mode else ""
            mode_opts += f'<option value="{m}" {sel}>{m}</option>'

        if mode == "live":
            mode_badge = '<span class="badge badge-ok">live</span>'
        elif mode == "paper":
            mode_badge = '<span class="badge badge-open">paper</span>'
        else:
            mode_badge = '<span class="badge badge-pass">observe</span>'

        body += f"""<tr>
<td><span style="color:{color};font-weight:700">{coin}</span></td>
<td>
  <form method="POST" action="/markets/set-mode" style="display:flex;gap:6px;align-items:center">
    <input type="hidden" name="coin" value="{coin}">
    <select name="mode" onchange="this.form.submit()">{mode_opts}</select>
  </form>
</td>
<td>Kalshi</td>
<td class="mkt-ticker">{ticker}</td>
<td>{yes_bid:.3f} / {yes_ask:.3f}</td>
<td class="muted">{poly_s}</td>
<td>{price_s}</td>
<td>{mode_badge}</td>
</tr>
"""
    body += '</table>\n</div>\n'
    return page_shell("Markets", "/markets", body, user=user)


# ── Providers page ──────────────────────────────────────────────────────────────
def build_providers_page(user=None):
    with _state_lock:
        health = dict(_health_status)

    now = int(time.time())

    def prow(name, key, docs=""):
        h = health.get(key, {})
        ok = h.get("ok", False)
        msg = h.get("msg", "no data yet")
        ts  = h.get("ts", 0)
        age = now - ts if ts else 9999
        age_s = f"{age}s ago" if ts else "never"
        dot_cls = "dot-ok" if ok else "dot-warn" if ts else "dot-err"
        status_cls = "green" if ok else "yellow" if ts else "red"
        return f"""<tr>
<td><span class="health-dot {dot_cls}"></span><strong>{name}</strong></td>
<td class="{status_cls}">{msg}</td>
<td class="muted">{age_s}</td>
<td><span class="badge {'badge-ok' if ok else 'badge-warn' if ts else 'badge-err'}">{'healthy' if ok else 'degraded' if ts else 'no data'}</span></td>
</tr>"""

    body  = '<div class="page-header">Providers</div>\n'
    body += '<div class="page-sub">API provider health and status.</div>\n'
    body += '<div class="card">\n'
    body += '<table class="trade-table"><tr><th>Provider</th><th>Status</th><th>Last Update</th><th>Health</th></tr>\n'
    body += prow("Coinbase (prices)", "coinbase")
    body += prow("Kalshi (trade venue)", "kalshi")
    body += prow("Polymarket (observe venue)", "polymarket")

    # MiniMax health — check key presence
    mini_ok = bool(MINIMAX_KEY)
    mini_msg = "Key configured" if mini_ok else "No API key"
    mini_ts  = health.get("minimax", {}).get("ts", 0)
    age_s = f"{now - mini_ts}s ago" if mini_ts else "never"
    dot   = "dot-ok" if mini_ok else "dot-err"
    scls  = "green" if mini_ok else "red"
    body += f'<tr><td><span class="health-dot {dot}"></span><strong>MiniMax (decisions)</strong></td><td class="{scls}">{mini_msg}</td><td class="muted">{age_s}</td><td><span class="badge {"badge-ok" if mini_ok else "badge-err"}">{"configured" if mini_ok else "missing"}</span></td></tr>'

    # Kalshi auth
    pem_ok = KALSHI_PEM.exists()
    pem_msg = "PEM loaded" if pem_ok else f"Missing: {KALSHI_PEM}"
    body += f'<tr><td><span class="health-dot {"dot-ok" if pem_ok else "dot-err"}"></span><strong>Kalshi Auth (RSA)</strong></td><td class="{"green" if pem_ok else "red"}">{pem_msg}</td><td class="muted">—</td><td><span class="badge {"badge-ok" if pem_ok else "badge-err"}">{"ok" if pem_ok else "error"}</span></td></tr>'
    body += '</table>\n</div>\n'

    # Config details
    body += '<div class="section-title">Configuration</div>\n<div class="card">\n'
    body += f'<div class="stat-row"><span class="stat-label">Kalshi Key ID</span><span class="stat-value muted" style="font-family:monospace">{KALSHI_KEY_ID[:20]}…</span></div>\n'
    body += f'<div class="stat-row"><span class="stat-label">MiniMax Model</span><span class="stat-value">{get_minimax_model()}</span></div>\n'
    body += f'<div class="stat-row"><span class="stat-label">MiniMax Key</span><span class="stat-value muted">{MINIMAX_KEY[:20] + "…" if MINIMAX_KEY else "missing"}</span></div>\n'
    body += f'<div class="stat-row"><span class="stat-label">Kalshi Base URL</span><span class="stat-value muted" style="font-family:monospace;font-size:11px">{KALSHI_BASE}</span></div>\n'
    body += f'<div class="stat-row"><span class="stat-label">Polymarket Base URL</span><span class="stat-value muted" style="font-family:monospace;font-size:11px">{POLYMARKET_BASE}</span></div>\n'
    body += '</div>\n'
    return page_shell("Providers", "/providers", body, user=user)


# ── Runs page ───────────────────────────────────────────────────────────────────
def build_runs_page(user=None, msg=""):
    conn = db_connect()
    runs = conn.execute("""
        SELECT id, name, coin, status, starting_capital, current_capital, wins, losses, total_pnl,
               started_at, ended_at, reset_reason
        FROM paper_runs ORDER BY id DESC LIMIT 100
    """).fetchall()
    conn.close()

    msg_html = f'<div style="background:#1a2f1a;border:1px solid #238636;color:#3fb950;padding:10px;border-radius:6px;margin-bottom:16px;font-size:13px">{msg}</div>' if msg else ""

    body  = f'<div class="page-header">Paper Runs {tooltip_html("paper_run")}</div>\n'
    body += '<div class="page-sub">Isolated paper trading experiments per coin. Archive a run to start fresh.</div>\n'
    body += msg_html

    # Quick archive buttons
    body += '<div class="card" style="margin-bottom:16px">\n'
    body += '<div style="font-weight:700;margin-bottom:10px">Start New Run</div>\n'
    body += '<div style="display:flex;gap:10px;flex-wrap:wrap">\n'
    for coin in COINS:
        color = COIN_COLORS[coin]
        body += f"""<form method="POST" action="/runs/archive">
  <input type="hidden" name="coin" value="{coin}">
  <button class="btn" type="submit" onclick="return confirm('Archive {coin} run and start fresh?')" style="border-left:3px solid {color}">Archive &amp; Reset {coin}</button>
</form>"""
    body += '</div>\n</div>\n'

    if not runs:
        body += '<div class="card"><div class="muted">No runs yet — runs are created automatically when the first decision fires.</div></div>'
    else:
        body += '<div class="card">\n'
        body += '<table class="trade-table"><tr><th>ID</th><th>Name</th><th>Coin</th><th>Status</th><th>Start $</th><th>Current $</th><th>W/L</th><th>P&amp;L</th><th>Started</th><th>Ended</th><th>Reason</th></tr>\n'
        for r in runs:
            rid, name, coin, status, start_cap, cur_cap, wins, losses, total_pnl, started_at, ended_at, reset_reason = r
            color = COIN_COLORS.get(coin, "#555")
            pnl_cls = "green" if total_pnl >= 0 else "red"
            status_badge = f'<span class="badge {"badge-ok" if status=="active" else "badge-pass"}">{status}</span>'
            started_s = started_at[:16] if started_at else "—"
            ended_s   = ended_at[:16] if ended_at else "—"
            reason_s  = (reset_reason or "—")[:20]
            body += f'<tr><td class="muted">#{rid}</td><td>{name}</td>'
            body += f'<td><span style="color:{color};font-weight:700">{coin}</span></td>'
            body += f'<td>{status_badge}</td><td>${start_cap:.0f}</td><td>${cur_cap:.2f}</td>'
            body += f'<td>{wins}/{losses}</td><td class="{pnl_cls}">${total_pnl:+.2f}</td>'
            body += f'<td class="muted" style="font-size:11px">{started_s}</td>'
            body += f'<td class="muted" style="font-size:11px">{ended_s}</td>'
            body += f'<td class="muted" style="font-size:11px">{reason_s}</td></tr>\n'
        body += '</table>\n</div>\n'
    return page_shell("Runs", "/runs", body, user=user)


# ── Audit page ──────────────────────────────────────────────────────────────────
def build_audit_page(user=None):
    conn = db_connect()
    logs = conn.execute("""
        SELECT id, actor, event_type, object_type, object_id, payload, created_at
        FROM audit_logs ORDER BY id DESC LIMIT 200
    """).fetchall()
    conn.close()

    body  = '<div class="page-header">Audit Log</div>\n'
    body += '<div class="page-sub">Record of all significant system events and configuration changes.</div>\n'
    if not logs:
        body += '<div class="card"><div class="muted">No audit events yet.</div></div>'
    else:
        body += '<div class="card">\n'
        body += '<table class="trade-table"><tr><th>Time (CT)</th><th>Actor</th><th>Event</th><th>Object</th><th>Details</th></tr>\n'
        for log in logs:
            lid, actor, event_type, object_type, object_id, payload, created_at = log
            ts_s = created_at[:16] if created_at else "?"
            obj_s = f"{object_type}/{object_id}" if object_type else "—"
            try:
                pay = json.loads(payload) if payload else {}
                pay_s = ", ".join(f"{k}={v}" for k, v in list(pay.items())[:3])
            except:
                pay_s = str(payload or "")[:50]
            body += f'<tr><td class="muted" style="font-size:11px">{ts_s}</td>'
            body += f'<td>{actor}</td><td><strong>{event_type}</strong></td>'
            body += f'<td class="muted" style="font-size:11px">{obj_s}</td>'
            body += f'<td class="muted" style="font-size:11px">{pay_s[:60]}</td></tr>\n'
        body += '</table>\n</div>\n'
    return page_shell("Audit", "/audit", body, user=user)


# ── Settings page ───────────────────────────────────────────────────────────────
def build_settings_page(user=None, msg=""):
    conn = db_connect()
    settings = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    accts  = conn.execute("SELECT coin, capital, wins, losses, total_pnl FROM paper_accounts").fetchall()
    rs_row = conn.execute("SELECT kill_switch, daily_loss_limit, max_drawdown_pct, max_stake, cooldown_after_losses FROM risk_settings WHERE id=1").fetchone()
    conn.close()

    rs = {"kill_switch": bool(rs_row[0]), "daily_loss_limit": rs_row[1],
          "max_drawdown_pct": rs_row[2], "max_stake": rs_row[3], "cooldown_after_losses": rs_row[4]} if rs_row else get_risk_settings()

    msg_html = f'<div style="background:#1a2f1a;border:1px solid #238636;color:#3fb950;padding:10px;border-radius:6px;margin-bottom:16px;font-size:13px">{msg}</div>' if msg else ""

    body  = '<div class="page-header">Settings</div>\n'
    body += '<div class="page-sub">Trading parameters, risk controls, and system configuration.</div>\n'
    body += msg_html

    # Paper trading
    body += '<div class="section-title">Paper Trading</div>\n<div class="card">\n'
    body += f'''<form method="POST" action="/settings/save">
<div class="form-row"><span class="form-label">Starting Capital / coin</span><input type="number" name="starting_capital" value="{settings.get('starting_capital', 500)}" step="0.01" style="width:120px"></div>
<div class="form-row"><span class="form-label">Min Stake ($)</span><input type="number" name="min_stake" value="{settings.get('min_stake', 20)}" step="1" style="width:100px"></div>
<div class="form-row"><span class="form-label">Max Stake ($)</span><input type="number" name="max_stake" value="{settings.get('max_stake', 30)}" step="1" style="width:100px"></div>
<div class="form-row"><span class="form-label">Decision Model</span><input type="text" name="model" value="{settings.get('model', get_minimax_model())}" style="width:220px">
<span class="muted" style="font-size:11px;margin-left:8px">MiniMax-M2.5 recommended — fast, no thinking overhead. M2.7 works but adds ~5s latency per decision.</span></div>
<div style="margin-top:14px"><button class="btn btn-primary" type="submit">Save Settings</button></div>
</form>'''
    body += '</div>\n'

    # Risk engine
    ks = rs["kill_switch"]
    ks_badge = '<span class="badge badge-err">ACTIVE</span>' if ks else '<span class="badge badge-ok">inactive</span>'
    body += f'<div class="section-title">Risk Engine {tooltip_html("kill_switch")}</div>\n<div class="card">\n'
    body += f'''<form method="POST" action="/settings/risk">
<div class="form-row">
  <span class="form-label">Kill Switch {tooltip_html("kill_switch")} {ks_badge}</span>
  <select name="kill_switch" style="width:100px">
    <option value="0" {"selected" if not ks else ""}>OFF</option>
    <option value="1" {"selected" if ks else ""}>ON</option>
  </select>
</div>
<div class="form-row"><span class="form-label">Daily Loss Limit ($) {tooltip_html("daily_loss_limit")}</span><input type="number" name="daily_loss_limit" value="{rs['daily_loss_limit']:.0f}" step="1" style="width:100px"></div>
<div class="form-row"><span class="form-label">Max Drawdown (%) {tooltip_html("max_drawdown_pct")}</span><input type="number" name="max_drawdown_pct" value="{rs['max_drawdown_pct']*100:.0f}" step="1" style="width:100px"></div>
<div class="form-row"><span class="form-label">Max Stake ($) {tooltip_html("max_drawdown_pct")}</span><input type="number" name="max_stake" value="{rs['max_stake']:.0f}" step="1" style="width:100px"></div>
<div class="form-row"><span class="form-label">Cooldown After N Losses {tooltip_html("cooldown_after_losses")}</span><input type="number" name="cooldown_after_losses" value="{rs['cooldown_after_losses']}" step="1" min="0" style="width:100px"></div>
<div class="form-row"><span class="form-label">Min Volume (contracts) {tooltip_html("min_volume")}</span><input type="number" name="min_volume" value="{settings.get('min_volume', 500)}" step="50" min="0" style="width:100px"><span style="color:#8b949e;font-size:11px;margin-left:8px">Skip windows below this 24h volume. 0 = disabled.</span></div>
<div class="form-row"><span class="form-label">Blackout Hours (CT)</span><input type="text" name="blackout_hours" value="{settings.get('blackout_hours', '8,10,11,17,18,23')}" style="width:200px" placeholder="e.g. 8,10,11,17"><span style="color:#8b949e;font-size:11px;margin-left:8px">Skip trading during these hours (CT, 0-23). Comma-separated. Based on your hour WR data.</span></div>
<div class="form-row"><span class="form-label">Auto-Pause WR Threshold</span><input type="number" name="autopause_wr_threshold" value="{settings.get('autopause_wr_threshold', 0.42)}" step="0.01" min="0" max="1" style="width:100px"><span style="color:#8b949e;font-size:11px;margin-left:8px">Pause coin if rolling WR (last 15 trades) falls below this. 0 = disabled. Suggested: 0.42</span></div>
<div class="form-row"><span class="form-label">Tracked Polymarket Wallets</span><input type="text" name="poly_tracked_wallets" value="{settings.get('poly_tracked_wallets', '')}" style="width:420px" placeholder="0xABC123...,0xDEF456... (comma-separated)"><span style="color:#8b949e;font-size:11px;margin-left:8px">Wallet addresses to copy-trade. Their recent buys/sells are shown to the LLM as smart money signals.</span></div>
<div style="margin-top:16px;padding-top:14px;border-top:1px solid #21262d">
  <div style="font-size:12px;font-weight:600;color:#58a6ff;margin-bottom:10px">Early Exit Engine</div>
  <div class="form-row"><span class="form-label">Take Profit (%)</span><input type="number" name="exit_take_profit_pct" value="{settings.get('exit_take_profit_pct', 40)}" step="5" min="0" style="width:100px"><span style="color:#8b949e;font-size:11px;margin-left:8px">Sell when unrealized P&L ≥ this % of stake. 0 = disabled.</span></div>
  <div class="form-row"><span class="form-label">Stop Loss (%)</span><input type="number" name="exit_stop_loss_pct" value="{settings.get('exit_stop_loss_pct', 65)}" step="5" min="0" style="width:100px"><span style="color:#8b949e;font-size:11px;margin-left:8px">Sell when losing ≥ this % of stake (recovers remaining value). 0 = disabled.</span></div>
  <div class="form-row"><span class="form-label">Time Cliff (secs)</span><input type="number" name="exit_time_cliff_secs" value="{settings.get('exit_time_cliff_secs', 90)}" step="10" min="0" style="width:100px"><span style="color:#8b949e;font-size:11px;margin-left:8px">Sell any winning position when this many seconds remain. 0 = disabled.</span></div>
  <div class="form-row"><span class="form-label">LLM Mid-Window Check</span><select name="exit_llm_check" style="width:100px"><option value="1" {"selected" if settings.get("exit_llm_check","1")=="1" else ""}>Enabled</option><option value="0" {"selected" if settings.get("exit_llm_check","1")=="0" else ""}>Disabled</option></select><span style="color:#8b949e;font-size:11px;margin-left:8px">Ask LLM to evaluate hold/sell at ~midpoint of window.</span></div>
</div>
<div style="margin-top:16px;padding-top:14px;border-top:1px solid #21262d">
  <div style="font-size:12px;font-weight:600;color:#58a6ff;margin-bottom:10px">Pool Mode — Multi-Position</div>
  <div class="form-row"><span class="form-label">Multi-Position Threshold</span><input type="number" name="pool_multi_threshold" value="{settings.get('pool_multi_threshold', 0)}" step="0.05" min="0" max="1" style="width:100px"><span style="color:#8b949e;font-size:11px;margin-left:8px">Pool score threshold to place multiple orders (0 = single winner only). e.g. 0.65 = place all coins scoring ≥ 0.65.</span></div>
</div>
<div style="margin-top:14px"><button class="btn btn-primary" type="submit">Save Risk Settings</button></div>
</form>'''
    body += '</div>\n'

    # Paper accounts
    body += '<div class="section-title">Paper Accounts</div>\n<div class="card">\n'
    body += '<table class="trade-table"><tr><th>Coin</th><th>Capital</th><th>Wins</th><th>Losses</th><th>P&amp;L</th><th>Action</th></tr>\n'
    for acct in accts:
        coin, capital, wins, losses, total_pnl = acct
        color   = COIN_COLORS.get(coin, "#555")
        pnl_cls = "green" if total_pnl >= 0 else "red"
        body += f'<tr><td><span style="color:{color};font-weight:700">{coin}</span></td>'
        body += f'<td>${capital:.2f}</td><td>{wins}</td><td>{losses}</td>'
        body += f'<td class="{pnl_cls}">${total_pnl:+.2f}</td>'
        body += f'<td><form method="POST" action="/settings/reset-coin" style="display:inline"><input type="hidden" name="coin" value="{coin}"><button class="btn" type="submit" onclick="return confirm(\'Reset {coin} account?\')">Reset</button></form></td></tr>\n'
    body += '</table>\n</div>\n'

    # Data import
    body += '<div class="section-title">Historical Data</div>\n<div class="card">\n'
    body += '<div class="stat-row"><span class="stat-label">Betbot historical data</span>'
    body += '<form method="POST" action="/settings/import-betbot" style="display:inline">'
    body += '<button class="btn btn-primary" type="submit">Import from betbot</button></form></div>\n'
    body += '<div class="muted" style="font-size:12px;margin-top:6px">Imports ~/autoresearch/data/kalshi_*_ticks.csv and kalshi_decisions*.json into autobet DB for all 4 coins.</div>\n'
    body += '</div>\n'

    # Credentials
    body += '<div class="section-title">Credentials</div>\n<div class="card">\n'
    def cred_row(label, has_it):
        dot = '<span class="health-dot dot-ok"></span>' if has_it else '<span class="health-dot dot-err"></span>'
        return f'<div class="stat-row"><span class="stat-label">{dot}{label}</span><span class="stat-value">{"Configured" if has_it else "Missing"}</span></div>\n'
    body += cred_row("MiniMax API Key", bool(MINIMAX_KEY))
    body += cred_row("Kalshi Key ID",   bool(KALSHI_KEY_ID))
    body += cred_row("Kalshi PEM File", KALSHI_PEM.exists())
    body += '</div>\n'

    return page_shell("Settings", "/settings", body, user=user)


# ── Health page ─────────────────────────────────────────────────────────────────
def build_health_page(user=None):
    now = int(time.time())
    with _state_lock:
        prices = dict(_prices)
        mkts   = {k: dict(v) for k, v in _active_mkts.items()}
        last_c = dict(_last_collect)
        health = dict(_health_status)

    conn = db_connect()
    price_count = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    tick_count  = conn.execute("SELECT COUNT(*) FROM kalshi_ticks").fetchone()[0]
    poly_count  = conn.execute("SELECT COUNT(*) FROM polymarket_ticks").fetchone()[0]
    trade_count = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    dec_count   = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    conn.close()

    body  = '<div class="page-header">Health</div>\n'
    body += '<div class="page-sub">System status, data feeds, and collection health.</div>\n'

    def dot(ok, warn=False):
        cls = "dot-ok" if ok and not warn else "dot-warn" if warn else "dot-err"
        return f'<span class="health-dot {cls}"></span>'

    body += '<div class="row">\n'
    body += '<div class="card">\n<div style="font-weight:700;margin-bottom:10px">Data Feeds</div>\n'
    for coin in COINS:
        price = prices.get(coin)
        lc    = last_c.get(coin, 0)
        age   = now - lc if lc else 9999
        ok    = age < 120
        warn  = 120 <= age < 300
        price_s = (f"${price:,.4f}" if price and price < 10 else f"${price:,.2f}") if price else "---"
        mkt   = mkts.get(coin, {})
        yes_bid = mkt.get("yes_bid", 0)
        yes_ask = mkt.get("yes_ask", 0)
        age_s = f"{age}s ago" if lc else "never"
        body += f'<div class="stat-row"><span class="stat-label">{dot(ok, warn)}{coin}</span>'
        body += f'<span class="stat-value">{price_s} &nbsp; {yes_bid:.3f}/{yes_ask:.3f} &nbsp; <span class="muted">{age_s}</span></span></div>\n'
    body += '</div>\n'

    body += '<div class="card">\n<div style="font-weight:700;margin-bottom:10px">System</div>\n'
    body += f'<div class="stat-row"><span class="stat-label">{dot(bool(MINIMAX_KEY))}MiniMax API</span><span class="stat-value">{"Configured" if MINIMAX_KEY else "Missing key"}</span></div>\n'
    body += f'<div class="stat-row"><span class="stat-label">{dot(KALSHI_PEM.exists())}Kalshi Auth</span><span class="stat-value">{"PEM loaded" if KALSHI_PEM.exists() else "Missing PEM"}</span></div>\n'
    body += f'<div class="stat-row"><span class="stat-label">{dot(True)}DB</span><span class="stat-value">{DB_PATH.name}</span></div>\n'
    body += f'<div class="stat-row"><span class="stat-label">{dot(True)}Price records</span><span class="stat-value">{price_count:,}</span></div>\n'
    body += f'<div class="stat-row"><span class="stat-label">{dot(True)}Kalshi ticks</span><span class="stat-value">{tick_count:,}</span></div>\n'
    body += f'<div class="stat-row"><span class="stat-label">{dot(True)}Polymarket ticks</span><span class="stat-value">{poly_count:,}</span></div>\n'
    body += f'<div class="stat-row"><span class="stat-label">{dot(True)}Decisions</span><span class="stat-value">{dec_count:,}</span></div>\n'
    body += f'<div class="stat-row"><span class="stat-label">{dot(True)}Paper trades</span><span class="stat-value">{trade_count:,}</span></div>\n'
    body += '</div>\n</div>\n'

    body += '<div class="section-title">Collection Log (last 20 Kalshi ticks)</div>\n<div class="card">\n'
    conn = db_connect()
    ticks = conn.execute("SELECT coin, window_ts, yes_bid, yes_ask, coin_price, ts FROM kalshi_ticks ORDER BY ts DESC LIMIT 20").fetchall()
    conn.close()
    if ticks:
        body += '<table class="trade-table"><tr><th>Time (CT)</th><th>Coin</th><th>Window</th><th>Bid</th><th>Ask</th><th>Price</th></tr>\n'
        for t in ticks:
            coin, wts, yes_bid, yes_ask, coin_price, ts = t
            color = COIN_COLORS.get(coin, "#555")
            t_str = ts_cst(ts).strftime("%H:%M:%S")
            w_str = ts_cst(wts).strftime("%H:%M") if wts else "?"
            cp = (f"${coin_price:,.4f}" if coin_price and coin_price < 10 else f"${coin_price:,.2f}") if coin_price else "?"
            body += f'<tr><td>{t_str}</td><td><span style="color:{color};font-weight:700">{coin}</span></td>'
            body += f'<td>{w_str}</td><td>{yes_bid:.3f}</td><td>{yes_ask:.3f}</td><td>{cp}</td></tr>\n'
        body += '</table>\n'
    else:
        body += '<div class="muted">No ticks yet — collection runs every 60 seconds.</div>\n'
    body += '</div>\n'
    return page_shell("Health", "/health", body, user=user)


# ── Replay page ──────────────────────────────────────────────────────────────────
def build_replay_page(user=None, msg="", run_id=None):
    conn = db_connect()
    body = '<div class="page-header">Replay</div>\n'
    body += '<div class="page-sub">Run a historical backtest using any engine against collected tick data.</div>\n'
    if msg:
        body += f'<div class="alert alert-ok">{msg}</div>\n'

    # Launch form
    body += '<div class="card">\n<div class="section-title">New Replay</div>\n'
    body += '<form method="POST" action="/replay/run">\n'
    body += '<div class="form-row"><span class="form-label">Coin</span><select name="coin" style="width:120px">'
    for c in COINS:
        body += f'<option value="{c}">{c}</option>'
    body += '</select></div>\n'
    body += '<div class="form-row"><span class="form-label">Engine</span><select name="engine_key" style="width:160px">'
    for ek, elabel in [("rules_engine","Rules Engine"),("vector_knn","Vector KNN"),
                       ("hybrid","Hybrid (Rules+KNN)"),("betbot_signal","Betbot Signal")]:
        body += f'<option value="{ek}">{elabel}</option>'
    body += '</select></div>\n'

    # Date range — default last 7 days
    now = int(time.time())
    d_from = ts_cst(now - 7*86400).strftime("%Y-%m-%d")
    d_to   = ts_cst(now).strftime("%Y-%m-%d")
    body += f'<div class="form-row"><span class="form-label">From</span><input type="date" name="date_from" value="{d_from}"></div>\n'
    body += f'<div class="form-row"><span class="form-label">To</span><input type="date" name="date_to" value="{d_to}"></div>\n'
    body += '<div class="form-row"><span class="form-label">Starting Capital ($)</span><input type="number" name="starting_capital" value="100" step="10" style="width:100px"></div>\n'
    body += '<button type="submit" class="btn">▶ Run Replay</button>\n</form>\n</div>\n'

    # Show selected run result
    if run_id:
        run = conn.execute("SELECT * FROM replay_runs WHERE id=?", (run_id,)).fetchone()
        if run:
            trades = conn.execute("""
                SELECT window_ts, direction, entry, size, pnl, result, balance
                FROM replay_trades WHERE replay_run_id=? ORDER BY window_ts
            """, (run_id,)).fetchall()
            total = len(trades)
            wins  = sum(1 for t in trades if t[5]=="WIN")
            fin_bal = trades[-1][6] if trades else float(run[6])
            pnl   = fin_bal - float(run[6])
            wr    = f"{wins/total*100:.1f}%" if total else "—"
            body += f'<div class="section-title">Result: {run[1]}</div>\n'
            body += '<div class="card">\n'
            body += f'<div class="stat-row"><span class="stat-label">Trades</span><span class="stat-value">{total}</span></div>\n'
            body += f'<div class="stat-row"><span class="stat-label">Win Rate</span><span class="stat-value">{wr}</span></div>\n'
            pnl_cls = "green" if pnl >= 0 else "red"
            body += f'<div class="stat-row"><span class="stat-label">Net P&L</span><span class="stat-value {pnl_cls}">${pnl:+.2f}</span></div>\n'
            body += f'<div class="stat-row"><span class="stat-label">Final Balance</span><span class="stat-value">${fin_bal:.2f}</span></div>\n'
            body += '</div>\n'
            if trades:
                body += '<div class="card" style="margin-top:12px;overflow-x:auto">\n'
                body += '<table class="trade-table"><tr><th>Time (CT)</th><th>Dir</th><th>Entry</th><th>Size</th><th>P&L</th><th>Result</th><th>Balance</th></tr>\n'
                for t in trades[-50:]:
                    wts2,direction,entry,size,pnl2,result,balance2 = t
                    t_str   = ts_cst(wts2).strftime("%m/%d %H:%M")
                    pnl_cls2= "green" if (pnl2 or 0)>=0 else "red"
                    badge   = f'<span class="badge badge-{"win" if result=="WIN" else "loss"}">{result}</span>'
                    body   += f'<tr><td>{t_str}</td><td>{direction}</td><td>{entry:.3f}</td>'
                    body   += f'<td>${size:.0f}</td><td class="{pnl_cls2}">${pnl2:+.2f}</td>'
                    body   += f'<td>{badge}</td><td>${balance2:.2f}</td></tr>\n'
                body += '</table>\n</div>\n'

    # Past runs list
    runs = conn.execute("""
        SELECT r.id, r.name, r.coin, r.engine_key, r.starting_capital, r.status, r.created_at,
               COUNT(t.id) trades, SUM(t.pnl) total_pnl
        FROM replay_runs r
        LEFT JOIN replay_trades t ON t.replay_run_id=r.id
        GROUP BY r.id ORDER BY r.created_at DESC LIMIT 20
    """).fetchall()
    conn.close()
    if runs:
        body += '<div class="section-title">Past Replays</div>\n<div class="card" style="overflow-x:auto">\n'
        body += '<table class="trade-table"><tr><th>Name</th><th>Coin</th><th>Engine</th><th>Trades</th><th>P&L</th><th>Status</th><th>View</th></tr>\n'
        for r in runs:
            rid,name,coin,ek,sc,status,cat,trades_n,tpnl = r
            pnl_cls = "green" if (tpnl or 0)>=0 else "red"
            body += f'<tr><td>{name}</td><td>{coin}</td><td>{ek}</td><td>{trades_n or 0}</td>'
            body += f'<td class="{pnl_cls}">${(tpnl or 0):+.2f}</td>'
            body += f'<td><span class="badge badge-{"ok" if status=="done" else "open"}">{status}</span></td>'
            body += f'<td><a href="/replay?run_id={rid}" class="btn" style="padding:3px 8px;font-size:12px">View</a></td></tr>\n'
        body += '</table>\n</div>\n'
    return page_shell("Replay", "/replay", body, user=user)


# ── Import Wizard page ────────────────────────────────────────────────────────────
def build_fill_quality_page(user=None):
    import datetime as _dt
    conn = db_connect()

    liq_rows = conn.execute(
        "SELECT coin, window_ts, ticker, direction, entry, requested_contracts, available_contracts, filled_contracts, liquidity_ok, created_at "
        "FROM fill_quality ORDER BY id DESC LIMIT 50"
    ).fetchall()
    import time as _time
    recent_2h = int(_time.time()) - 7200
    live_rows = conn.execute(
        "SELECT coin, window_ts, ticker, direction, contracts, limit_price, order_id, status, error, filled_contracts, avg_fill_price, created_at, pnl, actual_direction "
        "FROM live_orders WHERE status != 'failed' OR window_ts >= ? ORDER BY id DESC LIMIT 30",
        (recent_2h,)
    ).fetchall()
    conn.close()

    body = '<div class="page-header">Fill Quality</div>\n'
    body += '<div class="page-sub">Liquidity checks and live Kalshi order history.</div>\n'

    # ── Live Orders ──────────────────────────────────────────────────────────────
    body += '<div class="section-title" style="margin-top:4px">Live Orders</div>\n'
    if not live_rows:
        body += '<div class="card"><p class="muted" style="margin:0">No live orders yet — enable global live toggle and set a coin to live mode.</p></div>\n'
    else:
        body += '<div class="card" style="padding:0;overflow:hidden">\n'
        body += '''<div style="padding:6px 16px;border-bottom:1px solid #30363d;display:flex;align-items:center;gap:24px;background:#161b22">
  <div style="width:80px;font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.05em">Time</div>
  <div style="width:40px;font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.05em">Coin</div>
  <div style="width:36px;font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.05em">Dir</div>
  <div style="width:48px;font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.05em">Contracts</div>
  <div style="width:56px;font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.05em">Price</div>
  <div style="width:40px;font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.05em">Filled</div>
  <div style="flex:1;font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.05em">Order ID</div>
  <div style="width:60px;text-align:right;font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.05em">P&L</div>
  <div style="width:80px;text-align:right;font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.05em">Status</div>
</div>\n'''
        for r in live_rows:
            coin, wts, ticker, direction, contracts, limit_price, order_id, status, error, filled, avg_price, created_at, pnl, actual_dir = r
            try:
                ts = _dt.datetime.fromtimestamp(wts).strftime("%m/%d %H:%M")
            except:
                ts = str(wts)
            dir_color  = "#3fb950" if direction == "YES" else "#f85149"
            sc = {"filled": "#3fb950", "placed": "#e3b341", "failed": "#f85149", "canceled": "#8b949e"}.get(status, "#8b949e")
            icon = {"filled": "✓", "placed": "⏳", "failed": "✗", "canceled": "–"}.get(status, "?")
            oid_short  = (order_id or "")[:14] + "…" if order_id and len(order_id) > 14 else (order_id or "—")
            filled_s   = f"{filled}c" if filled is not None else "—"
            avg_s      = f"{avg_price*100:.0f}¢" if avg_price else f"{limit_price}¢ limit"
            err_s      = ""
            if status == "failed" and error:
                clean = error.replace('HTTP 401: {"error":{"code":"authentication_error","message":"authentication_error","details":"', "").rstrip('"} ')
                clean = clean.replace('HTTP 404: {"error":{"code":"market_not_found","message":"market not found","service":"exchange"}', "market not found")
                err_s = f'<div style="font-size:10px;color:#f85149;margin-top:2px">{clean[:80]}</div>'
            body += f'''<div style="padding:10px 16px;border-bottom:1px solid #21262d;display:flex;align-items:center;gap:24px">
  <div style="width:80px;font-size:11px;color:#8b949e">{ts}</div>
  <div style="width:40px;font-size:12px;font-weight:700;color:{COIN_COLORS.get(coin,"#ccc")}">{coin}</div>
  <div style="width:36px;font-size:11px;font-weight:600;color:{dir_color}">{direction}</div>
  <div style="width:48px;font-size:11px;color:#ccc">{contracts}c</div>
  <div style="width:56px;font-size:11px;color:#8b949e">{avg_s}</div>
  <div style="width:40px;font-size:11px;color:#8b949e">{filled_s}</div>
  <div style="flex:1;font-size:10px;color:#555;font-family:monospace">{oid_short}</div>
  <div style="width:60px;text-align:right;font-size:11px;font-weight:600;color:{"#3fb950" if pnl and pnl>0 else "#f85149" if pnl and pnl<0 else "#8b949e"}">{f"+${pnl:.2f}" if pnl and pnl>0 else f"-${abs(pnl):.2f}" if pnl and pnl<0 else "—"}</div>
  <div style="width:80px;text-align:right"><span style="font-size:11px;font-weight:600;color:{sc}">{icon} {status}</span>{err_s}</div>
</div>'''
        body += '</div>\n'

    # ── Liquidity Checks ─────────────────────────────────────────────────────────
    body += '<div class="section-title" style="margin-top:20px">Liquidity Checks</div>\n'
    if not liq_rows:
        body += '<div class="card"><p class="muted" style="margin:0">No liquidity check records yet.</p></div>\n'
    else:
        body += '<div class="card" style="overflow-x:auto">\n'
        body += '<table class="data-table">\n'
        body += '<tr><th>Time</th><th>Coin</th><th>Dir</th><th>Entry</th><th>Requested</th><th>Available</th><th>Filled</th><th>Status</th></tr>\n'
        for r in liq_rows:
            coin, wts, ticker, direction, entry, req, avail, filled, liq_ok, created_at = r
            try:
                ts = _dt.datetime.fromtimestamp(wts).strftime("%m/%d %H:%M")
            except:
                ts = str(wts)
            dir_color = "#3fb950" if direction == "YES" else "#f85149"
            req_s    = f"{req:.1f}" if req is not None else "—"
            avail_s  = f"{avail:.1f}" if avail is not None else "—"
            filled_s = f"{filled:.1f}" if filled is not None else "—"
            if liq_ok == 1:
                status_s = '<span style="color:#3fb950">✓ OK</span>'
            elif liq_ok == 0 and avail is not None and avail < 10:
                status_s = '<span style="color:#f85149">✗ SKIP (thin)</span>'
            else:
                status_s = '<span style="color:#e3b341">~ PARTIAL</span>'
            body += f'<tr><td>{ts}</td><td style="font-weight:700;color:{COIN_COLORS.get(coin,"#ccc")}">{coin}</td>'
            body += f'<td style="color:{dir_color};font-weight:600">{direction}</td>'
            body += f'<td>{entry:.4f}</td><td>{req_s}</td><td>{avail_s}</td><td>{filled_s}</td><td>{status_s}</td></tr>\n'
        body += '</table>\n</div>\n'

    return page_shell("Fill Quality", "/fill-quality", body, user=user)


def build_import_page(user=None, msg="", error=""):
    conn = db_connect()
    body = '<div class="page-header">Dataset Import</div>\n'
    body += '<div class="page-sub">Import historical tick data or price CSVs from the filesystem.</div>\n'
    if msg:
        body += f'<div class="alert alert-ok">{msg}</div>\n'
    if error:
        body += f'<div class="alert alert-err">{error}</div>\n'

    body += '<div class="card">\n<div class="section-title">Import from File Path</div>\n'
    body += '<form method="POST" action="/import/run">\n'
    body += '<div class="form-row"><span class="form-label">Source Type</span>'
    body += '<select name="source" style="width:180px">'
    body += '<option value="kalshi_csv">Kalshi Ticks CSV</option>'
    body += '<option value="price_csv">Price History CSV</option>'
    body += '</select></div>\n'
    body += '<div class="form-row"><span class="form-label">Coin</span><select name="coin" style="width:120px">'
    for c in COINS:
        body += f'<option value="{c}">{c}</option>'
    body += '</select></div>\n'
    body += '<div class="form-row"><span class="form-label">File Path on ryz</span>'
    body += '<input type="text" name="file_path" placeholder="/home/sean/autoresearch/data/kalshi_ticks.csv" style="width:420px"></div>\n'
    body += '<button type="submit" class="btn">▶ Start Import</button>\n</form>\n</div>\n'

    # Quick import from betbot data dir
    body += '<div class="card" style="margin-top:12px">\n<div class="section-title">Quick Import from Betbot</div>\n'
    body += '<p class="muted" style="font-size:13px">Import all 4 coins from ~/autoresearch/data/ in one click.</p>\n'
    body += '<form method="POST" action="/settings/import-betbot">'
    body += '<button type="submit" class="btn">⬇ Import Betbot Data</button></form>\n</div>\n'

    # Job history
    jobs = conn.execute("""
        SELECT id, source, file_path, status, records_imported, error_msg, created_at, completed_at
        FROM import_jobs ORDER BY created_at DESC LIMIT 20
    """).fetchall()
    conn.close()
    if jobs:
        body += '<div class="section-title" style="margin-top:16px">Import History</div>\n'
        body += '<div class="card" style="overflow-x:auto">\n'
        body += '<table class="trade-table"><tr><th>Source</th><th>File</th><th>Status</th><th>Records</th><th>Started</th><th>Error</th></tr>\n'
        for j in jobs:
            jid,src,fp,status,count,err,cat,comp = j
            fp_short = (fp or "")[-40:] if fp else "—"
            badge_cls = "badge-ok" if status=="done" else ("badge-loss" if status=="error" else "badge-open")
            body += f'<tr><td>{src}</td><td style="font-size:11px">{fp_short}</td>'
            body += f'<td><span class="badge {badge_cls}">{status}</span></td>'
            body += f'<td>{count or 0}</td><td style="font-size:11px">{(cat or "")[:16]}</td>'
            body += f'<td style="font-size:11px;color:#f85149">{(err or "")[:40]}</td></tr>\n'
        body += '</table>\n</div>\n'
    return page_shell("Import", "/import", body, user=user)


# ── Engine Manager page ───────────────────────────────────────────────────────────
def build_engines_page(user=None, msg=""):
    conn = db_connect()
    assignments = {r[0]: r[1] for r in conn.execute("SELECT coin, engine_key FROM market_group_engines").fetchall()}
    conn.close()
    body = '<div class="page-header">Engine Manager</div>\n'
    body += '<div class="page-sub">Choose which decision engine fires for each coin. Changes take effect on the next window.</div>\n'
    if msg:
        body += f'<div class="alert alert-ok">{msg}</div>\n'
    engines = [
        ("minimax_llm",   "MiniMax LLM",
         "Sends the Kalshi order book snapshot, current coin price, and recent tick history to MiniMax M2.5 via API. Returns a direction (YES/NO) and entry price. Highest signal quality — responds to real market context each window. Costs ~$0.001/call and takes 2–5 seconds. Best choice when API is reliable."),
        ("rules_engine",  "Rules Engine",
         "Simple threshold logic: if Kalshi YES mid > 0.62, bet YES. If mid < 0.38, bet NO. Otherwise PASS. Zero latency, no API calls, always available. Good baseline to compare against smarter engines. Ignores price momentum and order book depth."),
        ("vector_knn",    "Vector KNN",
         "Finds the 10 most similar historical 15-min windows using 8-feature cosine similarity (yes_bid, yes_ask, spread, volume, price change, momentum, etc.) and votes on direction. No API calls. Gets smarter as trade history grows — needs at least 20 resolved outcomes to be useful."),
        ("hybrid",        "Hybrid (Rules+KNN)",
         "Two-signal gate: Rules Engine must agree with KNN before a trade fires. Both have to point the same direction. More conservative than either alone — fires less often but with higher combined confidence. Good for reducing noise when KNN history is thin."),
        ("betbot_signal", "Betbot Signal",
         "Reads signal files that betbot's autoresearch loop writes to ~/autoresearch/data/kalshi_signals*.json. The loop uses MiniMax M2.7 to rewrite and score the kalshi_analyze.py strategy script every few windows based on live P&L. Most sophisticated option — the strategy self-improves over time. Requires betbot running on ryz.local."),
    ]
    # Build JS map of engine descriptions
    import json as _json
    engine_desc_js = _json.dumps({ek: d for ek,_,d in engines})
    body += f'<script>var ENGINE_DESCS={engine_desc_js};function updDesc(coin,sel){{var d=document.getElementById("desc_"+coin);if(d)d.textContent=ENGINE_DESCS[sel.value]||"";}}</script>\n'
    body += '<form method="POST" action="/engines/save">\n'
    body += '<div class="card">\n'
    body += '<table class="trade-table"><tr><th>Coin</th><th>Engine</th><th>Description</th></tr>\n'
    for coin in COINS:
        current = assignments.get(coin, "minimax_llm")
        body += f'<tr><td style="font-weight:700;color:{COIN_COLORS[coin]}">{coin}</td><td>'
        body += f'<select name="engine_{coin}" style="width:180px" onchange="updDesc(\'{coin}\',this)">'
        for ek, elabel, _ in engines:
            sel = ' selected' if ek == current else ''
            body += f'<option value="{ek}"{sel}>{elabel}</option>'
        body += '</select></td><td class="muted" style="font-size:12px">'
        desc = next((d for ek,_,d in engines if ek==current), "")
        body += f'<span id="desc_{coin}">{desc}</span></td></tr>\n'
    body += '</table>\n</div>\n'
    body += '<button type="submit" class="btn" style="margin-top:12px">Save Engine Assignments</button>\n</form>\n'

    # Engine reference card
    body += '<div class="section-title" style="margin-top:20px">Engine Reference</div>\n<div class="card">\n'
    for ek, elabel, desc in engines:
        body += f'<div style="padding:10px 0;border-bottom:1px solid #21262d">'
        body += f'<div style="font-size:13px;font-weight:600;color:#e6edf3;margin-bottom:3px"><code style="color:#58a6ff">{ek}</code> &mdash; {elabel}</div>'
        body += f'<div style="font-size:12px;color:#8b949e;line-height:1.5">{desc}</div>'
        body += f'</div>\n'
    body += '</div>\n'

    # Engine status table
    body += '<div class="section-title" style="margin-top:20px">Engine Status</div>\n<div class="card">\n'
    body += '<table class="trade-table"><tr><th>Engine</th><th>Label</th><th>Notes</th></tr>\n'
    for ek, elabel, desc in engines:
        if ek == "vector_knn":
            # Check how many outcomes we have
            try:
                conn2 = db_connect()
                outcomes = conn2.execute("SELECT COUNT(*) FROM decisions WHERE outcome IS NOT NULL").fetchone()[0]
                conn2.close()
                status_note = f"{outcomes} outcomes in DB" + (" ✓ ready" if outcomes >= 20 else " ⚠ need 20+")
            except Exception:
                status_note = "unknown"
        elif ek == "betbot_signal":
            found = sum(1 for p in BETBOT_SIGNAL_FILES.values() if os.path.exists(p))
            status_note = f"{found}/4 signal files present"
        else:
            status_note = "always available"
        body += f'<tr><td><code>{ek}</code></td><td>{elabel}</td><td class="muted" style="font-size:12px">{status_note}</td></tr>\n'
    body += '</table>\n</div>\n'
    return page_shell("Engines", "/engines", body, user=user)


# ── Research page ────────────────────────────────────────────────────────────────
def build_research_page(user=None):
    import glob
    ar = os.path.expanduser("~/autoresearch")
    body = '<div class="page-header">Research</div>\n'
    body += '<div class="page-sub">Autoresearch loop — MiniMax M2.7 evolves the strategy script each window.</div>\n'

    # Current analyze script
    body += '<div class="section-title">Current kalshi_analyze.py (BTC)</div>\n'
    body += '<div class="card" style="max-height:400px;overflow:auto">\n'
    try:
        with open(f"{ar}/kalshi_analyze.py") as f:
            content = f.read()
        body += f'<pre style="font-size:11px;white-space:pre-wrap">{content[:20000]}</pre>\n'
    except Exception:
        body += '<div class="muted">File not found — betbot research loop may not be running.</div>\n'
    body += '</div>\n'

    # Version history
    body += '<div class="section-title">Version History (last 10)</div>\n'
    body += '<div class="card">\n'
    hist = f"{ar}/kalshi_history"
    if os.path.exists(hist):
        files = sorted(glob.glob(f"{hist}/*.py"))
        if files:
            body += '<table class="trade-table"><tr><th>File</th><th>Score</th><th>Saved</th></tr>\n'
            for fp in files[-10:][::-1]:
                name = os.path.basename(fp)
                mtime = os.path.getmtime(fp)
                saved = ts_cst(int(mtime)).strftime("%m/%d %H:%M")
                # extract score from filename e.g. ..._score0.742.py
                score = "—"
                if "score" in name:
                    try:
                        score = name.split("score")[1].replace(".py", "")
                    except Exception:
                        pass
                body += f'<tr><td style="font-size:11px">{name}</td><td>{score}</td><td>{saved}</td></tr>\n'
            body += '</table>\n'
        else:
            body += '<div class="muted">No history files yet.</div>\n'
    else:
        body += '<div class="muted">History directory not found.</div>\n'
    body += '</div>\n'

    return page_shell("Research", "/research", body, user=user)


# ── Chat API ────────────────────────────────────────────────────────────────────
def handle_chat(message, user=None):
    """Grounded chat: fetch evidence from DB, send to MiniMax."""
    if not MINIMAX_KEY:
        return {"reply": "MiniMax API key not configured."}
    try:
        conn = db_connect()
        # Evidence: recent decisions
        decs = conn.execute("""
            SELECT d.coin, d.window_ts, d.direction, d.entry, d.confidence, d.rationale,
                   pt.result, pt.pnl
            FROM decisions d LEFT JOIN paper_trades pt ON pt.coin=d.coin AND pt.window_ts=d.window_ts
            ORDER BY d.window_ts DESC LIMIT 10
        """).fetchall()
        # Evidence: account state
        accts = conn.execute("SELECT coin, capital, wins, losses, total_pnl FROM paper_accounts").fetchall()
        # Evidence: risk settings
        rs = conn.execute("SELECT kill_switch, daily_loss_limit, max_drawdown_pct, max_stake FROM risk_settings WHERE id=1").fetchone()
        conn.close()

        dec_lines = []
        for d in decs:
            coin, wts, direction, entry, conf, rationale, result, pnl = d
            t_s = ts_cst(wts).strftime("%m/%d %H:%M") if wts else "?"
            res_s = result or "open"
            pnl_s = f"${pnl:+.2f}" if pnl is not None else "pending"
            dec_lines.append(f"  {t_s} {coin} {direction}@{entry:.3f} conf={conf:.0%} → {res_s} {pnl_s}: {(rationale or '')[:40]}")

        acct_lines = []
        for a in accts:
            coin, cap, wins, losses, pnl = a
            acct_lines.append(f"  {coin}: ${cap:.2f} W{wins}/L{losses} P&L ${pnl:+.2f}")

        with _state_lock:
            prices_s = ", ".join(f"{c}=${p:,.2f}" for c, p in _prices.items()) if _prices else "not collected"

        rs_s = f"kill_switch={'ON' if rs and rs[0] else 'OFF'}, daily_loss_limit=${rs[1] if rs else 100}, max_drawdown={rs[2]*100 if rs else 30}%" if rs else "default"

        context = f"""You are the Autobet trading platform assistant. You help marina staff and operators understand the platform.

=== PLATFORM OVERVIEW ===
Autobet is a multi-venue prediction market paper-trading platform. It collects live Kalshi and Polymarket tick data, runs a decision engine each 15-minute window, and simulates trades on paper. Real money is NOT at risk unless Live Mode is explicitly enabled (it is OFF by default).

=== PAGES ===
- Dashboard: Live coin cards with probability bars, open bets, P&L summary. Click a card to drill into that coin.
- Trades (/trades): Every paper trade with entry, size, open/close price, P&L.
- Decisions (/decisions): Every 15-min window — what the engine decided and why. Shows PASS (skipped), YES/NO trades, and the full rationale text from the engine.
- Insights (/insights): Performance analytics. Click a coin's "Drill down" button to filter to that coin only. Shows win rate, W/L counts, P&L trend, confidence calibration, and entry-price vs edge analysis.
- Markets (/markets): Live Kalshi + Polymarket data, execution mode per coin (observe/paper/live), global live toggle.
- Engines (/engines): Choose which decision engine fires per coin. Options: minimax_llm, rules_engine, vector_knn, hybrid, betbot_signal.
- Replay (/replay): Backtest any engine against historical tick data. No API calls. Pick coin, engine, date range.
- Import (/import): Import historical CSV data or trigger the betbot data import.
- Runs (/runs): Isolated paper trading experiments. Archive a run to start fresh. Archiving RESETS the daily-loss and cooldown counters for that coin.
- Research (/research): Shows the current evolved kalshi_analyze.py strategy script and version history from the autoresearch loop.
- Providers (/providers): Status of Kalshi, Coinbase, MiniMax, and Polymarket API connections.
- Audit (/audit): Log of all system actions.
- Settings (/settings): Trade size, model, risk limits. Run reset button per coin.
- Health (/health): Recent tick collection status.

=== DECISION ENGINES ===
- minimax_llm: Calls MiniMax API with market context each window. Best quality, uses tokens.
- rules_engine: If Kalshi mid > 0.62 → YES, mid < 0.38 → NO, else PASS. No API calls.
- vector_knn: Finds 10 most similar historical windows by 8-feature cosine similarity, votes direction.
- hybrid: Rules gate + KNN confidence boost.
- betbot_signal: Reads signal files from betbot's autoresearch loop (MiniMax M2.7 rewrites the strategy script each window based on P&L feedback). Most sophisticated.

=== PROBABILITY BARS ===
Each coin card on the dashboard shows a colored bar: shaded region = Kalshi bid/ask spread, white line = midpoint, blue marker = Polymarket price. Green = YES-leaning (>55%), Red = NO-leaning (<45%).

=== ACTIVE BETS ===
"YES @ 0.620 $25 stake" on a coin card means there is an open paper bet: direction=YES, entered at 0.620 (62 cents per contract), $25 staked. Click the badge or the card to go to /coin/BTC for full details.

=== EDGE% / EV ===
Edge% on the Insights confidence calibration table: "Edge" means that confidence bucket has >55% win rate historically. "Weak" = <45%. "Neutral" = between.
EV (expected value) on the entry price analysis = win_rate*(1-entry)*0.93 - loss_rate*entry. Positive = profitable at that entry price after fees.

=== RUN RESET ===
Archiving a run (Settings → Reset or Runs → Archive) saves the current run history and starts fresh. It resets: paper capital back to starting amount, W/L counters, daily-loss counter, cooldown streak counter. It does NOT change: kill switch, loss limits, drawdown %, max stake.

=== CURRENT STATE ===
Prices: {prices_s}

Recent decisions (last 10):
{chr(10).join(dec_lines) or "  None yet"}

Paper account balances:
{chr(10).join(acct_lines) or "  None yet"}

Risk settings: {rs_s}
Active model: {get_minimax_model()} | Fee: {KALSHI_FEE_RATE*100:.0f}% of entry*(1-entry)/contract (cap $0.02/contract)
Entry floor: {ENTRY_FLOOR} | Entry ceiling: {ENTRY_CEILING} | Max contracts: {MAX_CONTRACTS}

=== RISK GUARDRAILS ===
- Entry floor {ENTRY_FLOOR}: entries below this are blocked — they produce unrealistic contract counts (e.g. 38,000 contracts at $0.001) that can never fill at real Kalshi order book depth.
- Entry ceiling {ENTRY_CEILING}: entries above this are blocked — confirmed negative EV across all coins (entry 0.8+ = -$1,886 net in live data; momentum-chasing at near-certainty prices reverses at expiry).
- Max contracts {MAX_CONTRACTS}: cap on contracts per trade to reflect realistic order book liquidity.
- Fallback disabled: when the decision engine returns no signal, the system now records a PASS instead of blindly following market price. Fallback had 36-48% win rate across all coins.
- Ex-outlier P&L: shown on each coin card — total P&L minus the top 3 biggest wins. This strips lucky tail events to show underlying strategy performance. If negative, the strategy loses money without lucky strikes.
- Fee formula: corrected to min(0.07 * entry * (1-entry), 0.02) * contracts per Kalshi's official formula.

=== COIN PERFORMANCE NOTES (from analysis) ===
- XRP: most balanced YES/NO, best candidate for refinement. Both directions contribute.
- BTC: wins dominated by cheap-entry tail events (now blocked by entry floor). Normal trade quality unproven.
- SOL: NO-collapse setups most promising. YES side weak (41% WR historically).
- ETH: weakest general performance. Cooldown/pass mechanism firing correctly and preventing losses.

Answer the user's question concisely using the above context. If asked about a specific page or feature, explain it clearly."""

        payload = json.dumps({
            "model": get_minimax_model(),
            "max_tokens": 400,
            "messages": [
                {"role": "user", "content": f"{context}\n\nUser question: {message}"}
            ]
        }).encode()

        req = urllib.request.Request(
            MINIMAX_URL, data=payload,
            headers={"Content-Type": "application/json", "x-api-key": MINIMAX_KEY, "anthropic-version": "2023-06-01"}
        )
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.loads(r.read().decode())
            text = ""
            thinking_text = ""
            for block in resp.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "").strip()
                    break
                elif block.get("type") == "thinking" and not thinking_text:
                    thinking_text = block.get("thinking", "").strip()
            if not text and thinking_text:
                text = thinking_text
            # Log to audit
            with _state_lock:
                _health_status["minimax"] = {"ok": True, "msg": "Last chat response ok", "ts": int(time.time())}
            return {"reply": text or "No response text."}
    except Exception as e:
        return {"reply": f"Chat error: {e}"}

# ── HTTP Handler ────────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html, code=200):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def set_session_cookie(self, token):
        self.send_response(302)
        self.send_header("Set-Cookie", f"autobet_session={token}; Path=/; HttpOnly; Max-Age=604800")

    def require_auth(self):
        """Returns user dict or None (and sends redirect). Call at top of page handlers."""
        if not is_onboarding_complete():
            self.redirect("/onboarding")
            return None
        user = get_session(self)
        if not user:
            self.redirect("/login")
            return None
        return user

    def do_GET(self):
        path = self.path.split("?")[0]
        qs   = urllib.parse.parse_qs(self.path.split("?")[1] if "?" in self.path else "")

        # Public routes
        if path == "/login":
            self.send_html(build_login_page())
            return
        if path in ("/onboarding", "/onboarding/"):
            step = int(qs.get("step", ["1"])[0])
            self.send_html(build_onboarding_page(step=step))
            return
        if path == "/logo":
            if LOGO_PATH.exists():
                data = LOGO_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", len(data))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404); self.end_headers()
            return

        # Auth-required routes
        # API routes skip the onboarding redirect but still check session
        if path.startswith("/api/"):
            user = get_session(self) if is_onboarding_complete() else None
        else:
            user = self.require_auth()
            if user is None:
                return

        try:
            if path in ("/", "/dashboard"):
                self.send_html(build_dashboard(user=user))
            elif path == "/trades":
                self.send_html(build_trades_page(user=user))
            elif path == "/decisions":
                self.send_html(build_decisions_page(user=user))
            elif path == "/markets":
                self.send_html(build_markets_page(user=user))
            elif path == "/providers":
                self.send_html(build_providers_page(user=user))
            elif path == "/runs":
                self.send_html(build_runs_page(user=user))
            elif path == "/audit":
                self.send_html(build_audit_page(user=user))
            elif path == "/settings":
                msg = ""
                if "saved" in qs: msg = "Settings saved."
                if "reset" in qs: msg = "Account reset."
                if "risk" in qs:  msg = "Risk settings saved."
                if "msg"  in qs:  msg = urllib.parse.unquote(qs["msg"][0])
                self.send_html(build_settings_page(user=user, msg=msg))
            elif path == "/research":
                self.send_html(build_research_page(user=user))
            elif path == "/replay":
                run_id = qs.get("run_id", [None])[0]
                self.send_html(build_replay_page(user=user, run_id=int(run_id) if run_id else None))
            elif path == "/import":
                self.send_html(build_import_page(user=user))
            elif path == "/fill-quality":
                self.send_html(build_fill_quality_page(user=user))
            elif path == "/engines":
                self.send_html(build_engines_page(user=user))
            elif path == "/health":
                self.send_html(build_health_page(user=user))
            elif path == "/insights":
                coin_f = qs.get("coin", [None])[0]
                self.send_html(build_insights_page(user=user, coin_filter=coin_f))
            elif path.startswith("/coin/"):
                coin_f = path.split("/coin/")[-1].upper()
                self.send_html(build_coin_page(coin_f, user=user))
            elif path == "/auth/logout":
                self.send_response(302)
                self.send_header("Set-Cookie", "autobet_session=; Path=/; Max-Age=0")
                self.send_header("Location", "/login")
                self.end_headers()
            # API endpoints
            elif path == "/api/health":
                self.send_json({"status": "ok", "ts": int(time.time()), "model": get_minimax_model()})
            elif path == "/api/prices":
                with _state_lock:
                    self.send_json(_prices)
            elif path == "/api/markets":
                with _state_lock:
                    self.send_json({k: v for k, v in _active_mkts.items()})
            elif path == "/api/polymarket":
                with _state_lock:
                    self.send_json(_poly_mkts)
            elif path == "/api/accounts":
                conn = db_connect()
                rows = conn.execute("SELECT coin, capital, wins, losses, total_pnl, updated_at FROM paper_accounts").fetchall()
                conn.close()
                self.send_json([dict(r) for r in rows])
            elif path == "/api/trades":
                conn = db_connect()
                rows = conn.execute("""
                    SELECT coin, window_ts, direction, actual, entry, size, pnl, result, balance,
                           coin_open, coin_close, decided_at, resolved_at
                    FROM paper_trades ORDER BY window_ts DESC LIMIT 100
                """).fetchall()
                conn.close()
                self.send_json([dict(r) for r in rows])
            elif path == "/api/decisions":
                conn = db_connect()
                rows = conn.execute("""
                    SELECT coin, window_ts, direction, entry, confidence, rationale, decided_at
                    FROM decisions ORDER BY window_ts DESC LIMIT 50
                """).fetchall()
                conn.close()
                self.send_json([dict(r) for r in rows])
            elif path == "/api/stats":
                conn = db_connect()
                accts = conn.execute("SELECT coin, capital, wins, losses, total_pnl FROM paper_accounts").fetchall()
                total_trades = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE result IS NOT NULL").fetchone()[0]
                total_wins   = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE result='WIN'").fetchone()[0]
                conn.close()
                wr = total_wins / total_trades if total_trades > 0 else 0
                self.send_json({
                    "accounts": [dict(a) for a in accts],
                    "total_trades": total_trades,
                    "total_wins": total_wins,
                    "win_rate": round(wr, 4),
                    "starting_capital": STARTING_CAPITAL,
                })
            elif path == "/api/risk":
                self.send_json(get_risk_settings())
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as e:
            self.send_html(f"<pre>Error: {e}\n{traceback.format_exc()}</pre>", 500)

    def do_POST(self):
        path   = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length)

        # JSON API
        if self.headers.get("Content-Type", "").startswith("application/json"):
            try:
                params = json.loads(raw.decode())
            except:
                params = {}
        else:
            params = dict(urllib.parse.parse_qsl(raw.decode()))

        # Public POST routes
        if path == "/auth/login":
            username = params.get("username", "").strip()
            password = params.get("password", "")
            conn = db_connect()
            row = conn.execute("SELECT id, username, password_hash FROM users WHERE username=?", (username,)).fetchone()
            conn.close()
            if row and verify_password(password, row[2]):
                token = make_session_token(row[0], row[1])
                self.send_response(302)
                self.send_header("Set-Cookie", f"autobet_session={token}; Path=/; HttpOnly; Max-Age=604800")
                self.send_header("Location", "/")
                self.end_headers()
                audit("login", "user", username, actor=username)
            else:
                self.send_html(build_login_page(error="Invalid username or password."))
            return

        if path.startswith("/onboarding/step/"):
            step = int(path.split("/")[-1])
            if step == 1:
                username = params.get("username", "admin").strip()
                password = params.get("password", "")
                confirm  = params.get("confirm", "")
                if not password or password != confirm:
                    self.send_html(build_onboarding_page(step=1, error="Passwords do not match."))
                    return
                if len(password) < 4:
                    self.send_html(build_onboarding_page(step=1, error="Password must be at least 4 characters."))
                    return
                ph = hash_password(password)
                conn = db_connect()
                conn.execute("INSERT OR REPLACE INTO users (username, password_hash, role, created_at, updated_at) VALUES (?,?,?,?,?)",
                             (username, ph, "admin", now_cst().isoformat(), now_cst().isoformat()))

                conn.commit()
                conn.close()
                self.redirect("/onboarding?step=2")
            elif step == 2:
                self.redirect("/onboarding?step=3")
            elif step == 3:
                conn = db_connect()
                for k, v in [("starting_capital", params.get("starting_capital","500")),
                              ("min_stake",        params.get("trade_size","20")),
                              ("max_stake",        params.get("trade_size","20"))]:
                    conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?,?,?)",
                                 (k, v, now_cst().isoformat()))
                dl  = float(params.get("daily_loss_limit", 100))
                mdd = float(params.get("max_drawdown_pct", 30)) / 100.0
                conn.execute("UPDATE risk_settings SET daily_loss_limit=?, max_drawdown_pct=?, updated_at=? WHERE id=1",
                             (dl, mdd, now_cst().isoformat()))

                conn.commit()
                conn.close()
                self.redirect("/onboarding?step=4")
            elif step == 4:
                conn = db_connect()
                conn.execute("UPDATE system_state SET onboarding_complete=1, updated_at=? WHERE id=1",
                             (now_cst().isoformat(),))

                conn.commit()
                conn.close()
                audit("onboarding_complete", "system_state")
                self.send_html(build_onboarding_page(step=5, msg="Setup complete! Autobet is ready."))
            return

        # Auth-required POSTs
        user = get_session(self)
        if not user and is_onboarding_complete():
            if path == "/api/chat":
                self.send_json({"error": "Session expired — reload the page to log in again"}, 401)
            else:
                self.redirect("/login")
            return

        if path == "/settings/save":
            conn = db_connect()
            for key in ["starting_capital", "min_stake", "max_stake", "model"]:
                if key in params:
                    conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?,?,?)",
                                 (key, params[key], now_cst().isoformat()))

            conn.commit()
            conn.close()
            audit("settings_saved", "settings", payload=params, actor=user["username"] if user else "system")
            self.redirect("/settings?saved=1")

        elif path == "/settings/risk":
            conn = db_connect()
            ks  = int(params.get("kill_switch", 0))
            dll = float(params.get("daily_loss_limit", 100))
            mdd = float(params.get("max_drawdown_pct", 30)) / 100.0
            ms  = float(params.get("max_stake", 30))
            cal = int(params.get("cooldown_after_losses", 3))
            mv  = float(params.get("min_volume", 500))
            conn.execute("""
                UPDATE risk_settings SET kill_switch=?, daily_loss_limit=?, max_drawdown_pct=?,
                max_stake=?, cooldown_after_losses=?, updated_at=? WHERE id=1
            """, (ks, dll, mdd, ms, cal, now_cst().isoformat()))
            for key, default in [("min_volume", mv), ("min_confidence", float(params.get("min_confidence", 0.55))),
                                  ("exit_take_profit_pct", float(params.get("exit_take_profit_pct", 40))),
                                  ("exit_stop_loss_pct",   float(params.get("exit_stop_loss_pct", 65))),
                                  ("exit_time_cliff_secs", float(params.get("exit_time_cliff_secs", 90))),
                                  ("exit_llm_check",       params.get("exit_llm_check", "1")),
                                  ("pool_multi_threshold", float(params.get("pool_multi_threshold", 0))),
                                  ("blackout_hours",       params.get("blackout_hours", "8,10,11,17,18,23")),
                                  ("autopause_wr_threshold", float(params.get("autopause_wr_threshold", 0.42))),
                                  ("poly_tracked_wallets", params.get("poly_tracked_wallets", ""))]:
                conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?,?,?)",
                             (key, str(default), now_cst().isoformat()))
            conn.commit()
            conn.close()
            audit("risk_settings_saved", "risk_settings", payload={"kill_switch": ks, "daily_loss_limit": dll},
                  actor=user["username"] if user else "system")
            self.redirect("/settings?risk=1")

        elif path == "/settings/reset-coin":
            coin = params.get("coin", "").upper()
            if coin in COINS:
                conn = db_connect()
                cap_row = conn.execute("SELECT value FROM settings WHERE key='starting_capital'").fetchone()
                starting = float(cap_row[0]) if cap_row else STARTING_CAPITAL
                conn.execute("UPDATE paper_accounts SET capital=?, wins=0, losses=0, total_pnl=0, updated_at=? WHERE coin=?",
                             (starting, now_cst().isoformat(), coin))

                conn.commit()
                conn.close()
                audit("account_reset", "paper_accounts", coin, actor=user["username"] if user else "system")
            self.redirect("/settings?reset=1")

        elif path == "/markets/pool-toggle":
            conn = db_connect()
            row = conn.execute("SELECT value FROM settings WHERE key='pool_mode'").fetchone()
            new_val = "0" if (row and row[0] == "1") else "1"
            conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('pool_mode',?,?)",
                         (new_val, now_cst().isoformat()))
            conn.commit()
            conn.close()
            audit("pool_mode_toggle", "settings", payload={"pool_mode": new_val},
                  actor=user["username"] if user else "system")
            self.redirect("/markets")

        elif path == "/markets/live-toggle":
            conn = db_connect()
            row = conn.execute("SELECT global_live_enabled FROM system_state WHERE id=1").fetchone()
            new_val = 0 if (row and row[0] == 1) else 1
            conn.execute("UPDATE system_state SET global_live_enabled=?, updated_at=? WHERE id=1",
                         (new_val, now_cst().isoformat()))

            conn.commit()
            conn.close()
            audit("live_toggle", "system_state", payload={"global_live_enabled": new_val},
                  actor=user["username"] if user else "system")
            self.redirect("/markets")

        elif path == "/markets/set-mode":
            coin = params.get("coin", "").upper()
            mode = params.get("mode", "paper")
            if coin in COINS and mode in ("observe", "paper", "live"):
                conn = db_connect()
                conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?,?,?)",
                             (f"mode_{coin}", mode, now_cst().isoformat()))
                conn.execute("INSERT OR REPLACE INTO coin_modes (coin, mode, updated_at) VALUES (?,?,?)",
                             (coin, mode, now_cst().isoformat()))

                conn.commit()
                conn.close()
                audit("mode_change", "market_group", coin, payload={"mode": mode},
                      actor=user["username"] if user else "system")
            self.redirect("/markets")

        elif path == "/runs/archive":
            coin = params.get("coin", "").upper()
            if coin in COINS:
                archive_run(coin, reason="manual reset")
            self.redirect("/runs?archived=1")

        elif path == "/settings/import-betbot":
            msg = import_betbot_data()
            self.redirect(f"/settings?msg={urllib.parse.quote(msg)}")

        elif path == "/replay/run":
            coin       = params.get("coin", "BTC").upper()
            engine_key = params.get("engine_key", "rules_engine")
            date_from  = params.get("date_from", "")
            date_to    = params.get("date_to", "")
            starting_capital = float(params.get("starting_capital", 100))
            try:
                from datetime import datetime as _dt
                tz = _get_tz()
                start_ts = int(_dt.strptime(date_from, "%Y-%m-%d").replace(tzinfo=tz).timestamp())
                end_ts   = int(_dt.strptime(date_to,   "%Y-%m-%d").replace(tzinfo=tz).timestamp()) + 86400
            except Exception:
                start_ts = int(time.time()) - 7*86400
                end_ts   = int(time.time())
            # Run in background thread
            run_id_holder = [None]
            def _do_replay():
                run_id_holder[0] = run_replay(coin, engine_key, start_ts, end_ts, starting_capital)
            t = threading.Thread(target=_do_replay, daemon=True)
            t.start()
            t.join(timeout=30)
            rid = run_id_holder[0]
            if rid:
                self.redirect(f"/replay?run_id={rid}")
            else:
                self.redirect("/replay?msg=Replay+timed+out+or+no+data")

        elif path == "/import/run":
            source    = params.get("source", "kalshi_csv")
            file_path = params.get("file_path", "").strip()
            coin      = params.get("coin", "BTC").upper()
            if not file_path or not os.path.exists(file_path):
                self.redirect(f"/import?error={urllib.parse.quote('File not found: ' + file_path)}")
                return
            conn = db_connect()
            conn.execute("""
                INSERT INTO import_jobs (source, file_path, status, created_at)
                VALUES (?,?,'running',?)
            """, (source, file_path, now_cst().isoformat()))
            job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            conn.commit()
            conn.close()
            threading.Thread(target=run_import_job, args=(job_id, source, file_path, coin), daemon=True).start()
            self.redirect(f"/import?msg={urllib.parse.quote('Import started (job ' + str(job_id) + ')')}")

        elif path == "/engines/save":
            conn = db_connect()
            for coin in COINS:
                ek = params.get(f"engine_{coin}", "minimax_llm")
                conn.execute("""
                    INSERT OR REPLACE INTO market_group_engines (coin, engine_key, updated_at)
                    VALUES (?,?,?)
                """, (coin, ek, now_cst().isoformat()))

            conn.commit()
            conn.close()
            audit("engines_saved", "engines", payload=params, actor=user["username"] if user else "system")
            self.redirect("/engines?saved=1")

        elif path == "/api/chat":
            chat_user = get_session(self)
            if not chat_user and is_onboarding_complete():
                self.send_json({"error": "session expired — please reload and log in again"}, 401)
                return
            message = params.get("message", "")
            if not message:
                self.send_json({"error": "empty message"}, 400)
                return
            reply = handle_chat(message, user=chat_user)
            self.send_json(reply)

        else:
            self.send_json({"error": "not found"}, 404)


# ── Variable stake sizing (Kelly Criterion) ─────────────────────────────────────
def calc_stake(coin, confidence, capital, entry=0.5):
    """Half-Kelly bet sizing: f = (p - e) / (1 - e), scaled to half for safety.
    max_stake scales proportionally with capital growth above STARTING_CAPITAL."""
    conn = db_connect()
    min_s = float((conn.execute("SELECT value FROM settings WHERE key='min_stake'").fetchone() or [TRADE_SIZE])[0])
    max_s = float((conn.execute("SELECT value FROM settings WHERE key='max_stake'").fetchone() or [TRADE_SIZE * 1.5])[0])
    conn.close()
    # Feature 4: compound — scale max_stake with capital growth
    if capital > STARTING_CAPITAL:
        growth = capital / STARTING_CAPITAL
        max_s = max_s * growth
    p = float(confidence)
    e = float(entry) if entry and 0.05 < entry < 0.95 else 0.5
    # Kelly fraction: expected edge divided by potential gain
    kelly_f = max(0.0, (p - e) / (1.0 - e))
    # Half-Kelly for variance reduction
    half_kelly = kelly_f * 0.5
    # Convert fraction to dollar amount, capped at max_stake and 10% of capital
    size = capital * half_kelly
    size = max(min_s, min(size, max_s, capital * 0.10))
    return round(size, 2)

# ── Insights page ───────────────────────────────────────────────────────────────
def build_insights_page(user=None, coin_filter=None):
    conn = db_connect()
    coins_to_show = ([coin_filter] if coin_filter in COINS else COINS) if coin_filter else COINS
    coin_clause = f"AND coin='{coin_filter}'" if coin_filter in (COINS if coin_filter else []) else ""

    stats = {}
    for coin in coins_to_show:
        row = conn.execute("""
            SELECT COUNT(*) total,
                   SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) wins,
                   SUM(pnl) total_pnl, AVG(pnl) avg_pnl,
                   AVG(CASE WHEN result='WIN' THEN pnl END) avg_win,
                   AVG(CASE WHEN result='LOSS' THEN pnl END) avg_loss
            FROM paper_trades WHERE result IS NOT NULL AND coin=?
        """, (coin,)).fetchone()
        stats[coin] = row

    dir_stats = conn.execute(f"""
        SELECT coin, direction, COUNT(*) total,
               SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) wins, AVG(pnl) avg_pnl
        FROM paper_trades WHERE result IS NOT NULL {coin_clause}
        GROUP BY coin, direction
    """).fetchall()

    conf_stats = conn.execute(f"""
        SELECT d.coin, ROUND(d.confidence,1) cb, COUNT(*) total,
               SUM(CASE WHEN pt.result='WIN' THEN 1 ELSE 0 END) wins
        FROM decisions d
        JOIN paper_trades pt ON pt.coin=d.coin AND pt.window_ts=d.window_ts
        WHERE pt.result IS NOT NULL {coin_clause.replace('coin=', 'd.coin=')}
        GROUP BY d.coin, cb ORDER BY cb
    """).fetchall()

    entry_stats = conn.execute(f"""
        SELECT coin, ROUND(entry,1) bucket, COUNT(*) total,
               SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) wins, AVG(pnl) avg_pnl
        FROM paper_trades WHERE result IS NOT NULL {coin_clause}
        GROUP BY coin, bucket ORDER BY bucket
    """).fetchall()

    pnl_trend = conn.execute(f"""
        SELECT coin, window_ts, pnl, result FROM paper_trades
        WHERE result IS NOT NULL {coin_clause}
        ORDER BY window_ts DESC LIMIT 40
    """).fetchall()
    conn.close()

    title = f"Insights — {coin_filter}" if coin_filter else "Insights"
    body  = f'<div class="page-header">{title}</div>\n'
    body += '<div class="page-sub">Performance analytics, confidence calibration, and strategy signals.</div>\n'

    body += '<div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap">\n'
    body += f'<a href="/insights"><button class="btn {"btn-primary" if not coin_filter else ""}">All</button></a>\n'
    for coin in COINS:
        color  = COIN_COLORS[coin]
        active = coin == coin_filter
        body  += f'<a href="/insights?coin={coin}"><button class="btn" style="border-left:3px solid {color};{"background:#21262d;color:#58a6ff;" if active else ""}">{coin}</button></a>\n'
    body += '</div>\n'

    body += '<div class="row">\n'
    for coin in coins_to_show:
        row = stats.get(coin)
        if not row or not row[0]:
            continue
        color  = COIN_COLORS[coin]
        letter = COIN_LETTERS[coin]
        total, wins, total_pnl, avg_pnl, avg_win, avg_loss = row
        losses  = total - (wins or 0)
        wr      = (wins or 0) / total if total else 0
        exp_val = wr * (avg_win or 0) + (1 - wr) * (avg_loss or 0)
        pnl_cls = "green" if (total_pnl or 0) >= 0 else "red"
        body += f"""<div class="card">
  <div class="card-header"><div class="coin-badge" style="background:{color}">{letter}</div><div class="coin-name">{coin}</div></div>
  <div class="stat-row"><span class="stat-label">Win rate</span><span class="stat-value {'green' if wr>0.5 else 'red'}">{wr:.1%}</span></div>
  <div class="stat-row"><span class="stat-label">W / L</span><span class="stat-value">{wins} / {losses}</span></div>
  <div class="stat-row"><span class="stat-label">Total P&amp;L</span><span class="stat-value {pnl_cls}">${(total_pnl or 0):+.2f}</span></div>
  <div class="stat-row"><span class="stat-label">Avg P&amp;L</span><span class="stat-value {pnl_cls}">${(avg_pnl or 0):+.3f}</span></div>
  <div class="stat-row"><span class="stat-label">Expected value</span><span class="stat-value {'green' if exp_val>0 else 'red'}">${exp_val:+.4f}</span></div>
  <div style="margin-top:8px"><a href="/insights?coin={coin}"><button class="btn" style="font-size:11px">Drill down {tooltip_html("drill_down")}</button></a></div>
</div>"""
    body += '</div>\n'

    if not any(r and r[0] for r in stats.values()):
        body += '<div class="card"><div class="muted">No completed trades yet. Import betbot data or wait for the first window to resolve.</div></div>'
        return page_shell(title, "/insights", body, user=user)

    if dir_stats:
        body += '<div class="section-title">By Direction</div>\n<div class="card">\n'
        body += '<table class="trade-table"><tr><th>Coin</th><th>Direction</th><th>Trades</th><th>Win Rate</th><th>Avg P&amp;L</th><th>Signal</th></tr>\n'
        for coin, direction, total, wins, avg_pnl in dir_stats:
            color = COIN_COLORS.get(coin,"#555")
            wr    = (wins or 0)/total if total else 0
            sig   = "Prefer" if wr > 0.52 else "Avoid" if wr < 0.45 else "Neutral"
            sig_c = "green" if sig=="Prefer" else "red" if sig=="Avoid" else "yellow"
            body += f'<tr><td><span style="color:{color};font-weight:700">{coin}</span></td><td>{direction}</td><td>{total}</td>'
            body += f'<td class="{"green" if wr>0.5 else "red"}">{wr:.1%}</td>'
            body += f'<td class="{"green" if (avg_pnl or 0)>=0 else "red"}">${(avg_pnl or 0):+.3f}</td>'
            body += f'<td class="{sig_c}">{sig}</td></tr>\n'
        body += '</table>\n</div>\n'

    if conf_stats:
        body += f'<div class="section-title">Confidence Calibration {tooltip_html("confidence_calibration")}</div>\n<div class="card">\n'
        body += f'<table class="trade-table"><tr><th>Coin</th><th>Confidence {tooltip_html("confidence")}</th><th>Trades</th><th>Win Rate</th><th>Signal {tooltip_html("edge_pct")}</th></tr>\n'
        for coin, cb, total, wins in conf_stats:
            color = COIN_COLORS.get(coin,"#555")
            wr    = (wins or 0)/total if total else 0
            sig   = "Edge" if wr > 0.55 else "Weak" if wr < 0.45 else "Neutral"
            sig_c = "green" if sig=="Edge" else "red" if sig=="Weak" else "yellow"
            body += f'<tr><td><span style="color:{color};font-weight:700">{coin}</span></td><td>{cb:.0%}</td><td>{total}</td>'
            body += f'<td class="{"green" if wr>0.5 else "red"}">{wr:.1%}</td><td class="{sig_c}">{sig}</td></tr>\n'
        body += '</table>\n</div>\n'

    if entry_stats:
        body += f'<div class="section-title">Entry Price vs Edge {tooltip_html("entry_vs_edge")}</div>\n<div class="card">\n'
        body += f'<table class="trade-table"><tr><th>Coin</th><th>Entry</th><th>Trades</th><th>Win Rate</th><th>Avg P&amp;L</th><th>Fee-adj EV {tooltip_html("ev")}</th></tr>\n'
        for coin, bucket, total, wins, avg_pnl in entry_stats:
            if total < 2: continue
            color = COIN_COLORS.get(coin,"#555")
            wr    = (wins or 0)/total if total else 0
            e     = float(bucket or 0.5)
            ev    = wr*(1-e)*(1-KALSHI_FEE_RATE) - (1-wr)*e
            ev_c  = "green" if ev>0.01 else "yellow" if ev>-0.01 else "red"
            body += f'<tr><td><span style="color:{color};font-weight:700">{coin}</span></td><td>{e:.2f}</td><td>{total}</td>'
            body += f'<td class="{"green" if wr>0.5 else "red"}">{wr:.1%}</td>'
            body += f'<td class="{"green" if (avg_pnl or 0)>=0 else "red"}">${(avg_pnl or 0):+.3f}</td>'
            body += f'<td class="{ev_c}">EV {ev:+.3f}</td></tr>\n'
        body += '</table>\n</div>\n'

    if pnl_trend:
        body += '<div class="section-title">Recent P&L Trend (last 40 trades)</div>\n<div class="card">\n'
        body += '<table class="trade-table"><tr><th>Time (CT)</th><th>Coin</th><th>P&amp;L</th><th>Running Total</th><th>Result</th></tr>\n'
        rows_rev = list(reversed(pnl_trend))
        cum = 0.0
        for coin, wts, pnl, result in rows_rev:
            color = COIN_COLORS.get(coin,"#555")
            cum  += float(pnl or 0)
            t_s   = ts_cst(wts).strftime("%m/%d %H:%M") if wts else "?"
            bc    = "badge-win" if result=="WIN" else "badge-loss"
            body += f'<tr><td>{t_s}</td><td><span style="color:{color};font-weight:700">{coin}</span></td>'
            body += f'<td class="{"green" if (pnl or 0)>=0 else "red"}">${(pnl or 0):+.3f}</td>'
            body += f'<td class="{"green" if cum>=0 else "red"}">${cum:+.2f}</td>'
            body += f'<td><span class="badge {bc}">{result}</span></td></tr>\n'
        body += '</table>\n</div>\n'

    return page_shell(title, "/insights", body, user=user)


# ── Per-coin drill-down ─────────────────────────────────────────────────────────
def build_coin_page(coin, user=None):
    if coin not in COINS:
        return page_shell("Not Found", "/", '<div class="card"><div class="muted">Unknown coin.</div></div>', user=user)
    conn = db_connect()
    acct = conn.execute("SELECT capital, wins, losses, total_pnl FROM paper_accounts WHERE coin=?", (coin,)).fetchone()
    trades = conn.execute("""
        SELECT window_ts, direction, actual, entry, size, pnl, fee, result, coin_open, coin_close
        FROM paper_trades WHERE coin=? ORDER BY window_ts DESC LIMIT 50
    """, (coin,)).fetchall()
    decisions = conn.execute("""
        SELECT d.window_ts, d.direction, d.entry, d.confidence, d.rationale, pt.result, pt.pnl
        FROM decisions d LEFT JOIN paper_trades pt ON pt.coin=d.coin AND pt.window_ts=d.window_ts
        WHERE d.coin=? ORDER BY d.window_ts DESC LIMIT 30
    """, (coin,)).fetchall()
    ticks = conn.execute("""
        SELECT ts, window_ts, yes_bid, yes_ask, coin_price FROM kalshi_ticks
        WHERE coin=? ORDER BY ts DESC LIMIT 20
    """, (coin,)).fetchall()
    # Insights data for this coin
    dir_stats = conn.execute("""
        SELECT direction, COUNT(*) as total, SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
               AVG(pnl) as avg_pnl
        FROM paper_trades WHERE coin=? AND result IN ('WIN','LOSS')
        GROUP BY direction
    """, (coin,)).fetchall()
    entry_stats = conn.execute("""
        SELECT ROUND(entry,1) as bucket, COUNT(*) as total,
               SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
               AVG(pnl) as avg_pnl
        FROM paper_trades WHERE coin=? AND result IN ('WIN','LOSS') AND entry > 0
        GROUP BY bucket ORDER BY bucket
    """, (coin,)).fetchall()
    pnl_trend = conn.execute("""
        SELECT window_ts, pnl, result FROM paper_trades
        WHERE coin=? AND result IN ('WIN','LOSS') ORDER BY window_ts DESC LIMIT 20
    """, (coin,)).fetchall()
    conn.close()

    with _state_lock:
        price = _prices.get(coin)
        mkt   = _active_mkts.get(coin, {})
        poly  = _poly_mkts.get(coin, {})

    color   = COIN_COLORS[coin]
    letter  = COIN_LETTERS[coin]
    price_s = (f"${price:,.4f}" if price and price < 10 else f"${price:,.2f}") if price else "---"
    capital = acct[0] if acct else STARTING_CAPITAL
    wins    = acct[1] if acct else 0
    losses  = acct[2] if acct else 0
    tp      = acct[3] if acct else 0
    total_t = wins + losses
    wr      = f"{wins/total_t:.1%}" if total_t else "—"
    pnl_c   = "#3fb950" if tp >= 0 else "#f85149"

    body = f"""<div class="card" style="margin-bottom:16px;border-left:4px solid {color}">
  <div style="display:flex;align-items:center;gap:16px;margin-bottom:16px">
    <div class="coin-badge" style="background:{color};width:52px;height:52px;font-size:20px">{letter}</div>
    <div><div style="font-size:26px;font-weight:700">{coin}</div><div class="muted" style="font-size:12px">{mkt.get('ticker','—')}</div></div>
    <div style="margin-left:auto;text-align:right"><div style="font-size:32px;font-weight:700;color:#58a6ff">{price_s}</div><div class="muted" style="font-size:12px">Coinbase spot</div></div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px">
    <div style="background:#0d1117;border-radius:8px;padding:12px"><div class="muted" style="font-size:11px">Capital</div><div style="font-size:18px;font-weight:700">${capital:.2f}</div></div>
    <div style="background:#0d1117;border-radius:8px;padding:12px"><div class="muted" style="font-size:11px">Total P&L</div><div style="font-size:18px;font-weight:700;color:{pnl_c}">${tp:+.2f}</div></div>
    <div style="background:#0d1117;border-radius:8px;padding:12px"><div class="muted" style="font-size:11px">Win Rate</div><div style="font-size:18px;font-weight:700">{wr}</div></div>
    <div style="background:#0d1117;border-radius:8px;padding:12px"><div class="muted" style="font-size:11px">W / L</div><div style="font-size:18px;font-weight:700">{wins} / {losses}</div></div>
    <div style="background:#0d1117;border-radius:8px;padding:12px"><div class="muted" style="font-size:11px">Kalshi Bid/Ask</div><div style="font-size:16px;font-weight:700">{mkt.get('yes_bid',0):.3f} / {mkt.get('yes_ask',0):.3f}</div></div>
    <div style="background:#0d1117;border-radius:8px;padding:12px"><div class="muted" style="font-size:11px">Polymarket YES</div><div style="font-size:16px;font-weight:700">{f"{poly.get('yes_price'):.3f}" if poly.get('yes_price') else "—"}</div></div>
  </div>
</div>
<div style="display:flex;gap:8px;margin-bottom:16px">
  <a href="/insights?coin={coin}"><button class="btn btn-primary">Insights</button></a>
  <a href="/runs"><button class="btn">Runs</button></a>
  <form method="POST" action="/runs/archive" style="display:inline"><input type="hidden" name="coin" value="{coin}"><button class="btn btn-danger" type="submit" onclick="return confirm('Archive {coin} run?')">Reset Run</button></form>
</div>"""

    # Decisions
    body += '<div class="section-title">Decisions (last 30)</div>\n<div class="card" style="margin-bottom:16px">\n'
    if not decisions:
        body += '<div class="muted">No decisions yet.</div>'
    else:
        body += '<table class="trade-table"><tr><th>Time (CT)</th><th>Dir</th><th>Entry</th><th>Conf</th><th>Rationale</th><th>Result</th><th>P&L</th></tr>\n'
        for wts, direction, entry, conf, rationale, result, pnl in decisions:
            t_s    = ts_cst(wts).strftime("%m/%d %H:%M") if wts else "?"
            conf_s = f"{conf:.0%}" if conf else "—"
            rat    = rationale or ""
            is_fallback = "fallback" in rat.lower() or "Market favors" in rat
            is_blocked  = direction == "PASS"
            bc    = "badge-win" if result=="WIN" else "badge-loss" if result=="LOSS" else "badge-pass" if is_blocked else "badge-open"
            res_s = result or ("PASS" if is_blocked else "OPEN")
            pnl_s = f"${pnl:+.2f}" if pnl is not None else "—"
            pnl_c2 = "green" if (pnl or 0)>0 else "red" if (pnl or 0)<0 else "muted"
            row_style = ' style="opacity:0.5"' if is_fallback else ""
            fallback_tag = ' <span style="font-size:9px;color:#f85149;font-weight:700">FALLBACK</span>' if is_fallback else ""
            body += f'<tr{row_style}><td>{t_s}</td><td>{direction}{fallback_tag}</td><td>{entry:.3f}</td><td>{conf_s}</td>'
            rat_short2 = rat[:60] + ("…" if len(rat) > 60 else "")
            rat_esc2 = rat.replace("\\", "\\\\").replace("'", "\\'")
            click2 = f' onclick="showDetail(\'{rat_esc2}\')" style="cursor:pointer"' if len(rat) > 60 else ''
            body += f'<td class="muted" style="font-size:11px"{click2}>{rat_short2}</td>'
            body += f'<td><span class="badge {bc}">{res_s}</span></td><td class="{pnl_c2}">{pnl_s}</td></tr>\n'
        body += '</table>\n'
    body += '</div>\n'

    # Trades
    body += '<div class="section-title">Trades (last 50)</div>\n<div class="card" style="margin-bottom:16px">\n'
    if not trades:
        body += '<div class="muted">No trades yet.</div>'
    else:
        body += '<table class="trade-table"><tr><th>Time (CT)</th><th>Dir</th><th>Entry</th><th>Size</th><th>Open→Close</th><th>P&L</th><th>Result</th></tr>\n'
        for wts, direction, actual, entry, size, pnl, fee, result, co, cc in trades:
            t_s  = ts_cst(wts).strftime("%m/%d %H:%M") if wts else "?"
            co_s = f"${co:,.2f}" if co else "?"
            cc_s = f"${cc:,.2f}" if cc else "?"
            pnl_s= f"${pnl:+.2f}" if pnl is not None else "open"
            bc   = "badge-win" if result=="WIN" else "badge-loss" if result=="LOSS" else "badge-open"
            pc   = "green" if (pnl or 0)>0 else "red" if (pnl or 0)<0 else ""
            body += f'<tr><td>{t_s}</td><td>{direction}</td><td>{entry:.3f}</td><td>${size:.0f}</td>'
            body += f'<td>{co_s}→{cc_s}</td><td class="{pc}">{pnl_s}</td><td><span class="badge {bc}">{result or "OPEN"}</span></td></tr>\n'
        body += '</table>\n'
    body += '</div>\n'

    # Insights
    body += '<div class="section-title">Insights</div>\n'
    if not dir_stats and not entry_stats:
        body += '<div class="card"><div class="muted">No completed trades yet — insights appear after first resolved window.</div></div>\n'
    else:
        body += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px">\n'
        # Direction breakdown
        body += '<div class="card">\n<div style="font-weight:600;margin-bottom:10px;font-size:13px">Direction Breakdown</div>\n'
        if dir_stats:
            body += '<table class="trade-table"><tr><th>Dir</th><th>Trades</th><th>Win%</th><th>Avg P&L</th><th>Signal</th></tr>\n'
            for direction, total, wins, avg_pnl in dir_stats:
                wr = (wins or 0)/total if total else 0
                sig = "Prefer" if wr > 0.52 else "Avoid" if wr < 0.45 else "Neutral"
                sc  = "green" if sig=="Prefer" else "red" if sig=="Avoid" else "yellow"
                body += f'<tr><td><strong>{direction}</strong></td><td>{total}</td>'
                body += f'<td class="{"green" if wr>0.5 else "red"}">{wr:.0%}</td>'
                body += f'<td class="{"green" if (avg_pnl or 0)>=0 else "red"}">${(avg_pnl or 0):+.2f}</td>'
                body += f'<td class="{sc}">{sig}</td></tr>\n'
            body += '</table>\n'
        else:
            body += '<div class="muted" style="font-size:12px">No data</div>\n'
        body += '</div>\n'
        # P&L trend sparkline (text)
        body += '<div class="card">\n<div style="font-weight:600;margin-bottom:10px;font-size:13px">Recent P&L (last 20)</div>\n'
        if pnl_trend:
            rows_rev = list(reversed(pnl_trend))
            cum = 0.0
            body += '<div style="display:flex;flex-wrap:wrap;gap:3px;align-items:flex-end;margin-bottom:8px">\n'
            for wts_t, pnl_t, res_t in rows_rev:
                h = min(int(abs(pnl_t or 0) * 3 + 4), 28)
                c = "#238636" if (pnl_t or 0) >= 0 else "#da3633"
                cum += float(pnl_t or 0)
                body += f'<div style="width:8px;height:{h}px;background:{c};border-radius:2px" title="{res_t} ${(pnl_t or 0):+.2f}"></div>\n'
            body += '</div>\n'
            body += f'<div style="font-size:12px">Running P&L: <span class="{"green" if cum>=0 else "red"}">${cum:+.2f}</span></div>\n'
        else:
            body += '<div class="muted" style="font-size:12px">No data</div>\n'
        body += '</div>\n'
        body += '</div>\n'
        # Entry vs EV
        if entry_stats:
            body += '<div class="card" style="margin-bottom:16px">\n<div style="font-weight:600;margin-bottom:10px;font-size:13px">Entry Price vs EV {}</div>\n'.format(tooltip_html("entry_vs_edge"))
            body += '<table class="trade-table"><tr><th>Entry</th><th>Trades</th><th>Win%</th><th>Avg P&L</th><th>Fee-adj EV</th></tr>\n'
            for bucket, total, wins, avg_pnl in entry_stats:
                if total < 2: continue
                wr = (wins or 0)/total if total else 0
                e  = float(bucket or 0.5)
                ev = wr*(1-e)*(1-KALSHI_FEE_RATE) - (1-wr)*e
                ec = "green" if ev>0.01 else "yellow" if ev>-0.01 else "red"
                body += f'<tr><td>{e:.2f}</td><td>{total}</td>'
                body += f'<td class="{"green" if wr>0.5 else "red"}">{wr:.0%}</td>'
                body += f'<td class="{"green" if (avg_pnl or 0)>=0 else "red"}">${(avg_pnl or 0):+.2f}</td>'
                body += f'<td class="{ec}">EV {ev:+.3f}</td></tr>\n'
            body += '</table>\n</div>\n'

    # Live ticks
    body += '<div class="section-title">Live Ticks</div>\n<div class="card">\n'
    if ticks:
        body += '<table class="trade-table"><tr><th>Time (CT)</th><th>Window</th><th>Bid</th><th>Ask</th><th>Price</th></tr>\n'
        for ts_v, wts, yb, ya, cp in ticks:
            t_s = ts_cst(ts_v).strftime("%H:%M:%S")
            w_s = ts_cst(wts).strftime("%H:%M") if wts else "?"
            cp_s = (f"${cp:,.4f}" if cp and cp<10 else f"${cp:,.2f}") if cp else "?"
            body += f'<tr><td>{t_s}</td><td>{w_s}</td><td>{yb:.3f}</td><td>{ya:.3f}</td><td>{cp_s}</td></tr>\n'
        body += '</table>\n'
    else:
        body += '<div class="muted">No ticks yet.</div>'
    body += '</div>\n'
    return page_shell(f"{coin}", f"/coin/{coin}", body, user=user)


# ── Betbot data import ──────────────────────────────────────────────────────────
BETBOT_DATA = Path.home() / "autoresearch" / "data"
BETBOT_COIN_MAP = {
    "BTC": ("kalshi_ticks.csv",     "kalshi_decisions.json"),
    "ETH": ("kalshi_eth_ticks.csv", "kalshi_decisions_eth.json"),
    "SOL": ("kalshi_sol_ticks.csv", "kalshi_decisions_sol.json"),
    "XRP": ("kalshi_xrp_ticks.csv", "kalshi_decisions_xrp.json"),
}

def import_betbot_data():
    import csv as _csv
    total_ticks = 0
    total_decisions = 0
    conn = db_connect()
    for coin, (ticks_file, dec_file) in BETBOT_COIN_MAP.items():
        tf = BETBOT_DATA / ticks_file
        if tf.exists():
            with open(tf, newline="") as f:
                reader = _csv.DictReader(f)
                batch = []
                for row in reader:
                    try:
                        wts = int(row.get("window_ts", 0))
                        ts_raw = row.get("ts") or row.get("created_at", "")
                        try:
                            ts_val = int(float(ts_raw))
                        except:
                            from datetime import datetime as _dt
                            ts_val = int(_dt.fromisoformat(ts_raw.replace("Z","").split(".")[0]).replace(tzinfo=timezone.utc).timestamp())
                        yb = float(row.get("yes_bid") or 0)
                        ya = float(row.get("yes_ask") or 0)
                        if yb > 1: yb /= 100.0
                        if ya > 1: ya /= 100.0
                        lp = float(row.get("last_price") or 0)
                        if lp > 1: lp /= 100.0
                        cp_keys = ["btc_price","eth_price","sol_price","xrp_price","coin_price"]
                        cp = next((float(row[k]) for k in cp_keys if row.get(k)), None)
                        batch.append((coin, wts, row.get("market_ticker",""), yb, ya, lp,
                                      float(row.get("secs_left") or 0), cp, ts_val))
                    except:
                        continue
            if batch:
                conn.executemany("""INSERT OR IGNORE INTO kalshi_ticks
                    (coin,window_ts,market_ticker,yes_bid,yes_ask,last_price,secs_left,coin_price,ts)
                    VALUES (?,?,?,?,?,?,?,?,?)""", batch)
                total_ticks += len(batch)
                print(f"[IMPORT] {coin} ticks: {len(batch)}")
        df = BETBOT_DATA / dec_file
        if df.exists():
            try:
                decs = json.loads(df.read_text())
                batch_d = []
                for wts_str, v in decs.items():
                    d = v.get("dir","")
                    if d in ("YES","NO"):
                        batch_d.append((coin, int(wts_str), d, round(float(v.get("entry",0.5)),4),
                                        0.6, "betbot import", now_cst().isoformat()))
                if batch_d:
                    conn.executemany("""INSERT OR IGNORE INTO decisions
                        (coin,window_ts,direction,entry,confidence,rationale,decided_at)
                        VALUES (?,?,?,?,?,?,?)""", batch_d)
                    total_decisions += len(batch_d)
                    print(f"[IMPORT] {coin} decisions: {len(batch_d)}")
            except Exception as e:
                print(f"[IMPORT] {coin} decisions error: {e}")

    conn.commit()
    conn.close()
    msg = f"Imported {total_ticks} ticks, {total_decisions} decisions"
    audit("betbot_import", "datasets", payload={"ticks": total_ticks, "decisions": total_decisions})
    print(f"[IMPORT] {msg}")
    return msg

# ── Main ────────────────────────────────────────────────────────────────────────
def main():
    print("[AUTOBET] Starting up v2...")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db_init()

    # Start background threads
    threads = [
        threading.Thread(target=collect_prices,        daemon=True, name="prices"),
        threading.Thread(target=collect_kalshi,        daemon=True, name="kalshi"),
        threading.Thread(target=collect_polymarket,    daemon=True, name="polymarket"),
        threading.Thread(target=decision_loop,         daemon=True, name="decisions"),
        threading.Thread(target=live_order_sync_loop,  daemon=True, name="live_sync"),
    ]
    for t in threads:
        t.start()
    print(f"[AUTOBET] Threads: {[t.name for t in threads]}")

    # Prime price cache
    print("[AUTOBET] Priming prices...")
    for coin in COINS:
        p = fetch_coinbase_price(coin)
        if p:
            _prices[coin] = p
            print(f"  {coin}: ${p:,.2f}")

    onboarding = is_onboarding_complete()
    user_count = get_user_count()
    # If users exist but onboarding not marked complete, auto-complete it
    if not onboarding and user_count > 0:
        conn2 = db_connect()
        conn2.execute("UPDATE system_state SET onboarding_complete=1, updated_at=? WHERE id=1", (now_cst().isoformat(),))
        conn2.commit()
        conn2.close()
        onboarding = True
        print("[AUTOBET] Auto-completed onboarding (existing users found)")
    print(f"[AUTOBET] Onboarding complete: {onboarding} | Users: {user_count}")
    if not onboarding:
        print(f"[AUTOBET] First run — visit http://ryz.local:{PORT}/onboarding")
    else:
        print(f"[AUTOBET] Dashboard at http://ryz.local:{PORT}/")

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler) as httpd:
        httpd.serve_forever()

if __name__ == "__main__":
    main()
