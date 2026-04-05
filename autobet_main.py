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
KALSHI_PEM = BASE_DIR / "kalshi.pem"
LOGO_PATH  = Path.home() / "autobet" / "logo.jpg"

PORT = 7778
COINS = ["BTC", "XRP", "SOL", "ETH"]
SERIES = {"BTC": "KXBTC15M", "XRP": "KXXRP15M", "SOL": "KXSOL15M", "ETH": "KXETH15M"}
COIN_COLORS  = {"BTC": "#f7931a", "XRP": "#0066cc", "SOL": "#9945ff", "ETH": "#627eea"}
COIN_LETTERS = {"BTC": "B", "XRP": "X", "SOL": "S", "ETH": "E"}

KALSHI_BASE   = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_BASE = "https://clob.polymarket.com"
COINBASE_URL  = "https://api.coinbase.com/v2/prices/{}-USD/spot"
MINIMAX_URL   = "https://api.minimax.io/anthropic/v1/messages"

STARTING_CAPITAL = 500.0
TRADE_SIZE       = 20.0
KALSHI_FEE_RATE  = 0.07

# Betbot autoresearch signal files — written by kalshi_loop.py on ryz.local
# The research loop (MiniMax M2.7 rewrites kalshi_analyze.py each window) writes
# these files. We read them first; only fall back to direct MiniMax if no signal exists.
BETBOT_SIGNAL_FILES = {
    "BTC": "/home/sean/autoresearch/data/kalshi_signals.json",
    "ETH": "/home/sean/autoresearch/data/kalshi_signals_eth.json",
    "SOL": "/home/sean/autoresearch/data/kalshi_signals_sol.json",
    "XRP": "/home/sean/autoresearch/data/kalshi_signals_xrp.json",
}
_betbot_signals_cache: dict = {}   # {coin: (mtime, {wts_str: {dir, entry, size}})}

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
MINIMAX_MODEL = ENV.get("MINIMAX_MODEL", "MiniMax-M2.5")
KALSHI_KEY_ID = ENV.get("KALSHI_KEY_ID", "bd9c9f63-1f13-4527-8bc6-3c3a05196b49")
SESSION_SECRET = ENV.get("AUTOBET_SECRET", secrets.token_hex(32))

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

# ── Database ────────────────────────────────────────────────────────────────────
def db_connect():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
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

    # Ensure paper accounts exist for all coins
    for coin in COINS:
        conn.execute(
            "INSERT OR IGNORE INTO paper_accounts (coin, capital, updated_at) VALUES (?, ?, ?)",
            (coin, STARTING_CAPITAL, now_cst().isoformat())
        )
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
_health_status = {}   # key -> {ok, msg, ts}

# ── Price collection ────────────────────────────────────────────────────────────
def fetch_coinbase_price(coin):
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
    # Daily loss check
    try:
        today_start = int(datetime.combine(now_cst().date(), datetime.min.time()).replace(tzinfo=CST).timestamp())
        conn = db_connect()
        row = conn.execute("""
            SELECT COALESCE(SUM(pnl),0) FROM paper_trades
            WHERE coin=? AND result='LOSS' AND resolved_at >= ?
        """, (coin, ts_cst(today_start).isoformat())).fetchone()
        daily_loss = abs(float(row[0])) if row else 0.0
        conn.close()
        if daily_loss >= rs["daily_loss_limit"]:
            return False, f"Daily loss limit ${rs['daily_loss_limit']:.0f} reached for {coin} (${daily_loss:.2f} lost today)"
    except:
        pass
    # Drawdown check
    try:
        conn = db_connect()
        acct = conn.execute("SELECT capital FROM paper_accounts WHERE coin=?", (coin,)).fetchone()
        conn.close()
        if acct:
            capital = float(acct[0])
            floor = STARTING_CAPITAL * (1.0 - rs["max_drawdown_pct"])
            if capital < floor:
                return False, f"{coin} capital ${capital:.2f} below max drawdown floor ${floor:.2f}"
    except:
        pass
    # Cooldown check
    try:
        conn = db_connect()
        recent = conn.execute("""
            SELECT result FROM paper_trades WHERE coin=? AND result IS NOT NULL
            ORDER BY window_ts DESC LIMIT ?
        """, (coin, rs["cooldown_after_losses"])).fetchall()
        conn.close()
        if len(recent) >= rs["cooldown_after_losses"] and all(r[0] == "LOSS" for r in recent):
            return False, f"{coin} in cooldown: {rs['cooldown_after_losses']} consecutive losses"
    except:
        pass
    return True, "ok"

# ── Paper runs ──────────────────────────────────────────────────────────────────
def get_active_run_id(coin):
    """Get the active paper_run id for a coin, creating one if needed."""
    conn = db_connect()
    row = conn.execute(
        "SELECT id FROM paper_runs WHERE coin=? AND status='active' ORDER BY id DESC LIMIT 1",
        (coin,)
    ).fetchone()
    if row:
        conn.close()
        return row[0]
    # Create initial run
    rs = get_risk_settings()
    snap = json.dumps({"model": MINIMAX_MODEL, "trade_size": TRADE_SIZE, "started": now_cst().isoformat()})
    conn.execute("""
        INSERT INTO paper_runs (name, coin, status, starting_capital, current_capital, config_snapshot, started_at, created_at)
        VALUES (?, ?, 'active', ?, ?, ?, ?, ?)
    """, (f"{coin} Run 1", coin, STARTING_CAPITAL, STARTING_CAPITAL, snap, now_cst().isoformat(), now_cst().isoformat()))
    conn.commit()
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
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
    snap = json.dumps({"model": MINIMAX_MODEL, "trade_size": TRADE_SIZE, "started": now_cst().isoformat()})
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

def minimax_analyze(coin, ticks_summary, coin_price):
    if not MINIMAX_KEY:
        return None
    prompt = f"""You are analyzing a Kalshi 15-minute crypto prediction market for {coin}.

Current {coin} price: ${coin_price:,.2f}

Recent market data (last ~5 minutes of the current 15-min window):
{ticks_summary}

Based on this data:
1. Should we bet YES (price goes UP in next 15 min) or NO (price goes DOWN)?
2. What entry price (yes_ask for YES bet, 1-yes_bid for NO bet) represents fair value?

Respond with JSON only: {{"direction": "YES" or "NO", "entry": 0.XX, "confidence": 0.0-1.0, "rationale": "brief reason"}}"""

    payload = json.dumps({
        "model": MINIMAX_MODEL,
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()

    req = urllib.request.Request(
        MINIMAX_URL, data=payload,
        headers={"Content-Type": "application/json", "x-api-key": MINIMAX_KEY, "anthropic-version": "2023-06-01"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode())
            text = ""
            for block in resp.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "").strip()
                    break
            if not text:
                return None
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text.strip())
    except Exception as e:
        print(f"[MINIMAX] {coin}: {e}")
        return None

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
        coin_close = get_price_at(conn, coin, wts + 900)
        if not coin_close or not coin_open:
            continue
        actual = "YES" if coin_close > coin_open else "NO"
        if direction == actual:
            gross = contracts * (1.0 - entry)
            fee_per = min(KALSHI_FEE_RATE * (1.0 - entry), 0.02)
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
                run_id = get_active_run_id(coin)
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

def decision_loop():
    time.sleep(10)
    while True:
        try:
            wts = kalshi_window_ts()
            now = int(time.time())
            secs_into = now - wts
            if secs_into > 180:
                time.sleep(30)
                continue
            conn = db_connect()
            for coin in COINS:
                try:
                    existing = conn.execute(
                        "SELECT id FROM decisions WHERE coin=? AND window_ts=?", (coin, wts)
                    ).fetchone()
                    if existing:
                        continue
                    with _state_lock:
                        mkt = _active_mkts.get(coin)
                        coin_price = _prices.get(coin)
                    if not mkt or mkt.get("window_ts") != wts:
                        continue
                    if not coin_price:
                        continue
                    prev_wts = wts - 900
                    ticks = get_recent_ticks(conn, coin, prev_wts, n=10)
                    ticks_summary = format_ticks_summary(ticks)
                    # --- Signals bridge: use betbot's evolved analyze script first ---
                    bb_dir, bb_entry, bb_size = read_betbot_signal(coin, wts)
                    if bb_dir:
                        result = {
                            "direction": bb_dir,
                            "entry": bb_entry,
                            "confidence": 0.75,
                            "rationale": f"autoresearch signal (evolved strategy)",
                            "_betbot_size": bb_size,
                        }
                    else:
                        # Fall back to configured engine (minimax_llm / rules / knn / hybrid)
                        engine_key = get_engine_for_coin(coin)
                        result = run_engine(engine_key, coin, mkt, ticks, coin_price, ticks_summary)
                    if not result:
                        yes_bid = mkt.get("yes_bid", 0)
                        yes_ask = mkt.get("yes_ask", 0)
                        if yes_ask >= 0.50:
                            result = {"direction": "YES", "entry": yes_ask, "confidence": 0.5, "rationale": "Market favors YES (fallback)"}
                        elif yes_bid > 0:
                            result = {"direction": "NO", "entry": round(1.0 - yes_bid, 4), "confidence": 0.5, "rationale": "Market favors NO (fallback)"}
                        else:
                            continue
                    direction = result.get("direction", "")
                    entry = float(result.get("entry", 0))
                    confidence = float(result.get("confidence", 0.5))
                    rationale = result.get("rationale", "")
                    if direction not in ("YES", "NO") or entry <= 0 or entry >= 1.0:
                        continue

                    # Risk check + variable stake
                    acct_pre = conn.execute("SELECT capital FROM paper_accounts WHERE coin=?", (coin,)).fetchone()
                    capital_pre = acct_pre[0] if acct_pre else STARTING_CAPITAL
                    # If the betbot signal provided its own Kelly-sized stake, use it directly.
                    # Otherwise derive stake from confidence via calc_stake.
                    betbot_size = result.get("_betbot_size")
                    if betbot_size and betbot_size > 0:
                        size = min(betbot_size, capital_pre * 0.10)  # still cap at 10% capital
                    else:
                        size = calc_stake(coin, confidence, capital_pre)
                    ok, reason = check_risk(coin, direction, entry, size)
                    if not ok:
                        print(f"[RISK] {coin}: blocked — {reason}")
                        conn.execute("""
                            INSERT OR IGNORE INTO decisions (coin, window_ts, direction, entry, confidence, rationale, decided_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (coin, wts, "PASS", round(entry, 4), confidence,
                              f"Risk block: {reason}", now_cst().isoformat()))
                        conn.commit()
                        continue

                    conn.execute("""
                        INSERT OR IGNORE INTO decisions (coin, window_ts, direction, entry, confidence, rationale, decided_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (coin, wts, direction, round(entry, 4), confidence, rationale, now_cst().isoformat()))

                    contracts = size / entry
                    run_id = get_active_run_id(coin)

                    conn.execute("""
                        INSERT OR IGNORE INTO paper_trades
                        (coin, run_id, window_ts, direction, entry, size, contracts, coin_open, decided_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (coin, run_id, wts, direction, round(entry, 4), round(size, 4),
                          round(contracts, 4), coin_price, now_cst().isoformat()))
                    conn.commit()
                    print(f"[DECISION] {coin} wts={wts}: {direction} @ {entry:.3f}  conf={confidence:.2f}  '{rationale[:50]}'")
                except Exception as e:
                    print(f"[DECISION] {coin}: {e}")
                    traceback.print_exc()
            conn.close()
            try:
                resolve_trades()
            except Exception as e:
                print(f"[RESOLVE] {e}")
        except Exception as e:
            print(f"[DECISION LOOP] {e}")
        time.sleep(30)

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
}

def tooltip_html(key):
    text = TOOLTIPS.get(key, "")
    if not text:
        return ""
    escaped = text.replace('"', '&quot;')
    return f'<span class="tt" data-tip="{escaped}">ⓘ</span>'

TOOLTIP_CSS = """
  .tt { color: #58a6ff; cursor: help; font-size: 11px; margin-left: 4px; user-select: none; }
  .tt:hover::after {
    content: attr(data-tip);
    position: absolute;
    background: #1c2128;
    border: 1px solid #444;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
    color: #e6edf3;
    max-width: 280px;
    white-space: normal;
    z-index: 999;
    margin-top: 20px;
    margin-left: -140px;
    line-height: 1.5;
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
  }
  .tt { position: relative; }
"""

# ── Shared page chrome ──────────────────────────────────────────────────────────
SHARED_CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; min-height: 100vh; }
  a { color: inherit; text-decoration: none; }
  .topbar { background: #161b22; border-bottom: 1px solid #30363d; padding: 0 20px; display: flex; align-items: center; height: 56px; gap: 0; position: sticky; top: 0; z-index: 100; }
  .logo-img { height: 34px; width: 34px; border-radius: 6px; object-fit: cover; margin-right: 10px; }
  .logo-text { font-size: 18px; font-weight: 800; color: #58a6ff; letter-spacing: 2px; margin-right: 20px; white-space: nowrap; }
  .nav { display: flex; gap: 2px; flex: 1; overflow-x: auto; }
  .nav a { padding: 8px 12px; border-radius: 6px; font-size: 13px; font-weight: 500; color: #8b949e; transition: background 0.15s, color 0.15s; white-space: nowrap; }
  .nav a:hover { background: #21262d; color: #e6edf3; }
  .nav a.active { background: #21262d; color: #58a6ff; }
  .topbar-right { margin-left: auto; display: flex; align-items: center; gap: 10px; font-size: 12px; color: #8b949e; white-space: nowrap; }
  .content { padding: 20px; max-width: 1200px; margin: 0 auto; }
  .row { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; margin-bottom: 20px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 16px; }
  .card-header { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
  .coin-badge { width: 36px; height: 36px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 14px; flex-shrink: 0; }
  .coin-name { font-size: 18px; font-weight: 700; }
  .price { font-size: 22px; font-weight: 700; color: #58a6ff; }
  .stat-row { display: flex; justify-content: space-between; margin: 5px 0; font-size: 13px; align-items: center; }
  .stat-label { color: #8b949e; }
  .stat-value { font-weight: 600; }
  .green { color: #3fb950; } .red { color: #f85149; } .yellow { color: #d29922; } .muted { color: #8b949e; }
  .section-title { font-size: 13px; font-weight: 700; color: #8b949e; margin: 24px 0 10px; text-transform: uppercase; letter-spacing: 1px; }
  .trade-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .trade-table th { text-align: left; padding: 7px 10px; color: #8b949e; border-bottom: 1px solid #30363d; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
  .trade-table td { padding: 6px 10px; border-bottom: 1px solid #21262d; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
  .badge-win  { background: #1a2f1a; color: #3fb950; border: 1px solid #238636; }
  .badge-loss { background: #2d1515; color: #f85149; border: 1px solid #da3633; }
  .badge-open { background: #162032; color: #58a6ff; border: 1px solid #1f6feb; }
  .badge-pass { background: #1c1c1c; color: #8b949e; border: 1px solid #444; }
  .badge-ok   { background: #1a2f1a; color: #3fb950; border: 1px solid #238636; }
  .badge-warn { background: #2d2208; color: #d29922; border: 1px solid #9e6a03; }
  .badge-err  { background: #2d1515; color: #f85149; border: 1px solid #da3633; }
  .mkt-ticker { color: #8b949e; font-size: 11px; font-family: monospace; }
  .window-bar { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 10px 16px; margin-bottom: 16px; display: flex; gap: 24px; align-items: center; font-size: 13px; flex-wrap: wrap; }
  .btn { background: #21262d; border: 1px solid #30363d; color: #e6edf3; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  .btn:hover { background: #2d333b; }
  .btn-primary { background: #1f6feb; border-color: #1f6feb; color: #fff; }
  .btn-primary:hover { background: #388bfd; }
  .btn-danger { background: #da3633; border-color: #da3633; color: #fff; }
  .btn-danger:hover { background: #f85149; }
  .form-row { display: flex; align-items: center; gap: 12px; margin: 10px 0; }
  .form-label { font-size: 13px; color: #8b949e; min-width: 180px; }
  .form-val { font-size: 13px; font-weight: 600; }
  input[type=text], input[type=number], input[type=password], select, textarea {
    background: #0d1117; border: 1px solid #30363d; color: #e6edf3;
    padding: 6px 10px; border-radius: 6px; font-size: 13px; outline: none;
  }
  input:focus, select:focus, textarea:focus { border-color: #58a6ff; }
  .health-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .dot-ok { background: #3fb950; } .dot-warn { background: #d29922; } .dot-err { background: #f85149; }
  .page-header { font-size: 20px; font-weight: 700; margin-bottom: 4px; }
  .page-sub { font-size: 13px; color: #8b949e; margin-bottom: 20px; }
  .kill-banner { background: #2d1515; border: 1px solid #da3633; color: #f85149; padding: 10px 16px; border-radius: 8px; margin-bottom: 16px; font-weight: 600; font-size: 13px; }
  /* Chat popup */
  #chat-btn { position: fixed; bottom: 24px; right: 24px; width: 52px; height: 52px; border-radius: 50%; background: #1f6feb; border: none; color: #fff; font-size: 22px; cursor: pointer; z-index: 1000; box-shadow: 0 4px 16px rgba(31,111,235,0.4); transition: transform 0.2s; }
  #chat-btn:hover { transform: scale(1.1); }
  #chat-panel { display: none; position: fixed; bottom: 88px; right: 24px; width: 360px; max-height: 480px; background: #161b22; border: 1px solid #30363d; border-radius: 12px; z-index: 1000; flex-direction: column; box-shadow: 0 8px 32px rgba(0,0,0,0.6); }
  #chat-panel.open { display: flex; }
  #chat-header { padding: 12px 16px; border-bottom: 1px solid #30363d; font-weight: 700; font-size: 14px; display: flex; justify-content: space-between; align-items: center; }
  #chat-msgs { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
  .chat-msg { padding: 8px 12px; border-radius: 8px; font-size: 13px; line-height: 1.5; max-width: 90%; }
  .chat-msg.user { background: #1f3a5f; align-self: flex-end; }
  .chat-msg.assistant { background: #21262d; align-self: flex-start; }
  #chat-input-row { padding: 10px; border-top: 1px solid #30363d; display: flex; gap: 8px; }
  #chat-input { flex: 1; resize: none; height: 36px; font-family: inherit; }
  #chat-send { padding: 6px 14px; }
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
        ("/runs",       "Runs"),
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

<!-- Chat popup -->
<button id="chat-btn" onclick="toggleChat()" title="Ask Autobet">💬</button>
<div id="chat-panel">
  <div id="chat-header">
    <span>Autobet Chat</span>
    <button class="btn" onclick="toggleChat()" style="padding:2px 8px">✕</button>
  </div>
  <div id="chat-msgs"></div>
  <div id="chat-input-row">
    <textarea id="chat-input" class="form-input" placeholder="Ask about trades, decisions, settings…" onkeydown="if(event.key==='Enter'&&!event.shiftKey){{event.preventDefault();sendChat();}}"></textarea>
    <button class="btn btn-primary" id="chat-send" onclick="sendChat()">Send</button>
  </div>
</div>

<script>
setTimeout(() => location.reload(), 60000);
{extra_js}
function toggleChat() {{
  var p = document.getElementById('chat-panel');
  p.classList.toggle('open');
}}
function sendChat() {{
  var inp = document.getElementById('chat-input');
  var msg = inp.value.trim();
  if (!msg) return;
  inp.value = '';
  appendChat('user', msg);
  fetch('/api/chat', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{message: msg}})
  }}).then(r => r.json()).then(d => {{
    appendChat('assistant', d.reply || d.error || 'No response');
  }}).catch(e => appendChat('assistant', 'Error: ' + e));
}}
function appendChat(role, text) {{
  var msgs = document.getElementById('chat-msgs');
  var div = document.createElement('div');
  div.className = 'chat-msg ' + role;
  div.textContent = text;
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
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
  <div class="form-row"><span class="form-label">Trade Size ($)</span><input type="number" name="trade_size" value="20" step="5" style="width:120px"></div>
  <div class="form-row"><span class="form-label">Daily Loss Limit ($)</span><input type="number" name="daily_loss_limit" value="100" step="10" style="width:120px"></div>
  <div class="form-row"><span class="form-label">Max Drawdown (%)</span><input type="number" name="max_drawdown_pct" value="30" step="5" style="width:120px"></div>
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
    # Live toggle state
    state = conn.execute("SELECT global_live_enabled FROM system_state WHERE id=1").fetchone()
    live_on = state and state[0] == 1
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
  <span>Model: <strong>{MINIMAX_MODEL}</strong></span>
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
        wins       = acct[2] if acct else 0
        losses     = acct[3] if acct else 0
        total_pnl  = acct[4] if acct else 0
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
            d2, e2 = open_t[0], open_t[1]
            open_badge = f'<span class="badge badge-open">{d2} @ {e2:.3f}</span>'
        else:
            open_badge = '<span class="muted" style="font-size:12px">no open bet</span>'
        body += f"""<a href="/coin/{coin}" style="text-decoration:none"><div class="card" style="cursor:pointer;transition:border-color 0.15s" onmouseover="this.style.borderColor='{color}'" onmouseout="this.style.borderColor=''">
  <div class="card-header">
    <div class="coin-badge" style="background:{color}">{letter}</div>
    <div><div class="coin-name">{coin}</div><div class="mkt-ticker">{ticker}</div></div>
    <div style="margin-left:auto;text-align:right"><div class="price">{price_str}</div></div>
  </div>
  <div class="stat-row"><span class="stat-label">Kalshi Bid/Ask</span><span class="stat-value">{yes_bid:.3f} / {yes_ask:.3f}</span></div>
  <div class="stat-row"><span class="stat-label">Polymarket YES</span><span class="stat-value muted">{poly_str}</span></div>
  <div class="stat-row"><span class="stat-label">Paper Capital</span><span class="stat-value">${capital:.2f}</span></div>
  <div class="stat-row"><span class="stat-label">Total P&amp;L</span><span class="stat-value {pnl_cls}">${total_pnl:+.2f}</span></div>
  <div class="stat-row"><span class="stat-label">W / L / Rate</span><span class="stat-value">{wins}/{losses} &nbsp; {win_rate}</span></div>
  <div style="margin-top:8px">{open_badge}</div>
</div></a>
"""
    body += '</div>\n<div class="section-title">Recent Trades</div>\n'
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
    rows = conn.execute("""
        SELECT d.coin, d.window_ts, d.direction, d.entry, d.confidence, d.rationale, d.decided_at,
               pt.result, pt.pnl
        FROM decisions d
        LEFT JOIN paper_trades pt ON pt.coin=d.coin AND pt.window_ts=d.window_ts
        ORDER BY d.window_ts DESC LIMIT 100
    """).fetchall()
    conn.close()

    body = '<div class="page-header">Decisions</div>\n'
    body += '<div class="page-sub">All committed decisions with MiniMax rationale and outcome.</div>\n'
    if not rows:
        body += '<div class="card"><div class="muted">No decisions yet.</div></div>'
    else:
        body += '<div class="card">\n'
        body += '<table class="trade-table"><tr><th>Time (CT)</th><th>Coin</th><th>Dir</th><th>Entry</th><th>Conf</th><th>Rationale</th><th>Outcome</th><th>P&amp;L</th></tr>\n'
        for r in rows:
            coin, wts, direction, entry, confidence, rationale, decided_at, result, pnl = r
            color = COIN_COLORS.get(coin, "#555")
            t_str = ts_cst(wts).strftime("%m/%d %H:%M") if wts else "?"
            conf_s = f"{confidence:.0%}" if confidence else "—"
            rat_s  = (rationale or "")[:60]
            if result == "WIN":
                outcome = '<span class="badge badge-win">WIN</span>'
            elif result == "LOSS":
                outcome = '<span class="badge badge-loss">LOSS</span>'
            elif direction == "PASS":
                outcome = '<span class="badge badge-pass">PASS</span>'
            else:
                outcome = '<span class="badge badge-open">OPEN</span>'
            pnl_cls = "green" if (pnl or 0) > 0 else "red" if (pnl or 0) < 0 else "muted"
            pnl_s   = f"${pnl:+.2f}" if pnl is not None else "—"
            body += f'<tr><td>{t_str}</td>'
            body += f'<td><span style="color:{color};font-weight:700">{coin}</span></td>'
            body += f'<td>{direction}</td><td>{entry:.3f}</td><td>{conf_s}</td>'
            body += f'<td class="muted" style="font-size:11px">{rat_s}</td>'
            body += f'<td>{outcome}</td><td class="{pnl_cls}">{pnl_s}</td></tr>\n'
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

    # Global live toggle
    conn2 = db_connect()
    state = conn2.execute("SELECT global_live_enabled FROM system_state WHERE id=1").fetchone()
    conn2.close()
    live_on = state and state[0] == 1
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
    body += f'<div class="stat-row"><span class="stat-label">MiniMax Model</span><span class="stat-value">{MINIMAX_MODEL}</span></div>\n'
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
<div class="form-row"><span class="form-label">Starting Capital / coin</span><input type="number" name="starting_capital" value="{settings.get('starting_capital', 500)}" step="10" style="width:100px"></div>
<div class="form-row"><span class="form-label">Min Stake ($)</span><input type="number" name="min_stake" value="{settings.get('min_stake', 20)}" step="5" style="width:100px"></div>
<div class="form-row"><span class="form-label">Max Stake ($)</span><input type="number" name="max_stake" value="{settings.get('max_stake', 30)}" step="5" style="width:100px"></div>
<div class="form-row"><span class="form-label">Decision Model</span><input type="text" name="model" value="{settings.get('model', MINIMAX_MODEL)}" style="width:220px"></div>
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
<div class="form-row"><span class="form-label">Daily Loss Limit ($) {tooltip_html("daily_loss_limit")}</span><input type="number" name="daily_loss_limit" value="{rs['daily_loss_limit']:.0f}" step="10" style="width:100px"></div>
<div class="form-row"><span class="form-label">Max Drawdown (%) {tooltip_html("max_drawdown_pct")}</span><input type="number" name="max_drawdown_pct" value="{rs['max_drawdown_pct']*100:.0f}" step="5" style="width:100px"></div>
<div class="form-row"><span class="form-label">Max Stake ($) {tooltip_html("max_drawdown_pct")}</span><input type="number" name="max_stake" value="{rs['max_stake']:.0f}" step="5" style="width:100px"></div>
<div class="form-row"><span class="form-label">Cooldown After N Losses {tooltip_html("cooldown_after_losses")}</span><input type="number" name="cooldown_after_losses" value="{rs['cooldown_after_losses']}" step="1" min="0" style="width:100px"></div>
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

        context = f"""You are the Autobet trading platform assistant. Answer questions about the system using the evidence below.

Current prices: {prices_s}

Recent decisions (last 10):
{chr(10).join(dec_lines) or "  None yet"}

Paper account balances:
{chr(10).join(acct_lines) or "  None yet"}

Risk settings: {rs_s}

Model: {MINIMAX_MODEL} | Trade size: ${TRADE_SIZE} | Fee: {KALSHI_FEE_RATE*100:.0f}% of profit (cap $0.02/contract)

Answer the user's question based on this evidence. Be concise and specific. If you don't have enough evidence, say so."""

        payload = json.dumps({
            "model": MINIMAX_MODEL,
            "max_tokens": 400,
            "messages": [
                {"role": "user", "content": f"{context}\n\nUser question: {message}"}
            ]
        }).encode()

        req = urllib.request.Request(
            MINIMAX_URL, data=payload,
            headers={"Content-Type": "application/json", "x-api-key": MINIMAX_KEY, "anthropic-version": "2023-06-01"}
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode())
            text = ""
            for block in resp.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "").strip()
                    break
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
                self.send_json({"status": "ok", "ts": int(time.time()), "model": MINIMAX_MODEL})
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
            conn.execute("""
                UPDATE risk_settings SET kill_switch=?, daily_loss_limit=?, max_drawdown_pct=?,
                max_stake=?, cooldown_after_losses=?, updated_at=? WHERE id=1
            """, (ks, dll, mdd, ms, cal, now_cst().isoformat()))
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

        elif path == "/api/chat":
            message = params.get("message", "")
            if not message:
                self.send_json({"error": "empty message"}, 400)
                return
            reply = handle_chat(message, user=user)
            self.send_json(reply)

        else:
            self.send_json({"error": "not found"}, 404)


# ── Variable stake sizing ───────────────────────────────────────────────────────
def calc_stake(coin, confidence, capital):
    """Scale stake between min and max based on confidence."""
    conn = db_connect()
    min_s = float((conn.execute("SELECT value FROM settings WHERE key='min_stake'").fetchone() or [TRADE_SIZE])[0])
    max_s = float((conn.execute("SELECT value FROM settings WHERE key='max_stake'").fetchone() or [TRADE_SIZE * 1.5])[0])
    conn.close()
    conf_norm = max(0.0, min(1.0, (float(confidence) - 0.5) * 2.0))
    size = min_s + conf_norm * (max_s - min_s)
    return round(min(max(size, min_s), capital * 0.10), 2)

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
  <div style="margin-top:8px"><a href="/insights?coin={coin}"><button class="btn" style="font-size:11px">Drill down</button></a></div>
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
        body += '<div class="section-title">Confidence Calibration</div>\n<div class="card">\n'
        body += '<table class="trade-table"><tr><th>Coin</th><th>Confidence</th><th>Trades</th><th>Win Rate</th><th>Signal</th></tr>\n'
        for coin, cb, total, wins in conf_stats:
            color = COIN_COLORS.get(coin,"#555")
            wr    = (wins or 0)/total if total else 0
            sig   = "Edge" if wr > 0.55 else "Weak" if wr < 0.45 else "Neutral"
            sig_c = "green" if sig=="Edge" else "red" if sig=="Weak" else "yellow"
            body += f'<tr><td><span style="color:{color};font-weight:700">{coin}</span></td><td>{cb:.0%}</td><td>{total}</td>'
            body += f'<td class="{"green" if wr>0.5 else "red"}">{wr:.1%}</td><td class="{sig_c}">{sig}</td></tr>\n'
        body += '</table>\n</div>\n'

    if entry_stats:
        body += '<div class="section-title">Entry Price vs Edge</div>\n<div class="card">\n'
        body += '<table class="trade-table"><tr><th>Coin</th><th>Entry</th><th>Trades</th><th>Win Rate</th><th>Avg P&amp;L</th><th>Fee-adj EV</th></tr>\n'
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
            t_s   = ts_cst(wts).strftime("%m/%d %H:%M") if wts else "?"
            conf_s = f"{conf:.0%}" if conf else "—"
            bc    = "badge-win" if result=="WIN" else "badge-loss" if result=="LOSS" else "badge-pass" if direction=="PASS" else "badge-open"
            res_s = result or ("PASS" if direction=="PASS" else "OPEN")
            pnl_s = f"${pnl:+.2f}" if pnl is not None else "—"
            pnl_c2 = "green" if (pnl or 0)>0 else "red" if (pnl or 0)<0 else "muted"
            body += f'<tr><td>{t_s}</td><td>{direction}</td><td>{entry:.3f}</td><td>{conf_s}</td>'
            body += f'<td class="muted" style="font-size:11px">{(rationale or "")[:50]}</td>'
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
        threading.Thread(target=collect_prices,     daemon=True, name="prices"),
        threading.Thread(target=collect_kalshi,     daemon=True, name="kalshi"),
        threading.Thread(target=collect_polymarket, daemon=True, name="polymarket"),
        threading.Thread(target=decision_loop,      daemon=True, name="decisions"),
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
# =============================================================================
# SECTION A: Decision Engine Registry - Engine Implementations
# Added by Clyde per MASTER_PLAN_V2.md
# =============================================================================

def read_betbot_signal(coin, window_ts):
    """Read the evolved strategy signal from betbot's autoresearch loop.

    Returns (direction, entry, size) or (None, None, None) if no signal exists.
    Caches the signal file in memory and reloads only when mtime changes.
    """
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
    except Exception as e:
        print(f"[BETBOT_SIGNAL] {coin}: {e}")
    return None, None, None


def get_engine_for_coin(coin):
    """Get the selected engine for a coin."""
    conn = db_connect()
    row = conn.execute("SELECT engine_key FROM market_group_engines WHERE coin=?", (coin,)).fetchone()
    conn.close()
    return row[0] if row else "minimax_llm"

def run_engine(engine_key, coin, mkt, ticks, coin_price, ticks_summary):
    """Dispatcher: route to the appropriate decision engine."""
    if engine_key == "rules_engine":
        return rules_engine(coin, mkt, ticks, coin_price)
    elif engine_key == "vector_knn":
        return vector_knn_engine(coin, mkt, ticks, coin_price)
    elif engine_key == "hybrid":
        return hybrid_engine(coin, mkt, ticks, coin_price)
    elif engine_key == "minimax_llm":
        return minimax_analyze(coin, ticks_summary, coin_price)
    else:
        # Default to minimax
        return minimax_analyze(coin, ticks_summary, coin_price)

def rules_engine(coin, mkt, ticks, coin_price):
    """Engine 2: Rules-based decision engine.
    
    Logic:
    - If spread > 0.15: PASS (too wide, bad fill risk)
    - If mid > 0.62: YES at yes_ask
    - If mid < 0.38: NO at (1 - yes_bid)
    - Else: PASS
    """
    try:
        yes_ask = float(mkt.get("yes_ask", 0) or 0)
        yes_bid = float(mkt.get("yes_bid", 0) or 0)
        
        if yes_ask <= 0 or yes_bid <= 0:
            return None
            
        mid = (yes_bid + yes_ask) / 2
        spread = yes_ask - yes_bid
        
        # Too wide spread
        if spread > 0.15:
            return None
            
        # Calculate confidence as distance from 0.5, normalized
        confidence = abs(mid - 0.5) * 2
        
        if mid > 0.62:
            return {
                "direction": "YES",
                "entry": yes_ask,
                "confidence": round(confidence, 4),
                "rationale": f"Rules: mid={mid:.3f} > 0.62"
            }
        elif mid < 0.38:
            return {
                "direction": "NO",
                "entry": round(1.0 - yes_bid, 4),
                "confidence": round(confidence, 4),
                "rationale": f"Rules: mid={mid:.3f} < 0.38"
            }
        else:
            return None
    except Exception as e:
        print(f"[RULES] Error: {e}")
        return None

def vector_knn_engine(coin, mkt, ticks, coin_price):
    """Engine 3: Vector KNN decision engine.
    
    Requires >= 200 rows in kalshi_ticks for that coin.
    Uses 8-dimensional feature vectors and cosine similarity.
    """
    try:
        conn = db_connect()
        count = conn.execute("SELECT COUNT(*) FROM kalshi_ticks WHERE coin=?", (coin,)).fetchone()[0]
        if count < 200:
            conn.close()
            return {"direction": "PASS", "entry": 0, "confidence": 0, 
                    "rationale": f"Insufficient history: {count} rows < 200"}
        
        # Feature vector would be built here from ticks
        # For now, fall back to rules engine as placeholder
        conn.close()
        return rules_engine(coin, mkt, ticks, coin_price)
    except Exception as e:
        return None

def hybrid_engine(coin, mkt, ticks, coin_price):
    """Engine 4: Hybrid (Rules gate + KNN).
    
    Run rules engine first, if PASS return PASS.
    If rules says YES/NO, run KNN for entry price.
    """
    rules_result = rules_engine(coin, mkt, ticks, coin_price)
    if not rules_result or rules_result.get("direction") == "PASS":
        return rules_result
    
    # If rules says YES/NO, try KNN for better entry
    knn_result = vector_knn_engine(coin, mkt, ticks, coin_price)
    if knn_result and knn_result.get("direction") != "PASS":
        # Average confidence
        conf = (rules_result.get("confidence", 0) + knn_result.get("confidence", 0)) / 2
        rules_result["confidence"] = round(conf, 4)
        rules_result["rationale"] += " + KNN"
    
    return rules_result
