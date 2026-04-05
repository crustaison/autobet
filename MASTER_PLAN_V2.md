# Autobet — Master Plan V2
## Completion Roadmap for Remaining Features
### For Clyde (and anyone else implementing this)

---

## ⚠️ CRITICAL INSTRUCTIONS FOR CLYDE BEFORE TOUCHING ANYTHING

**READ THIS ENTIRE SECTION BEFORE WRITING A SINGLE LINE OF CODE.**

This codebase is a single Python file: `~/autobet/autobet_main.py`. It is currently ~2700 lines and fully working. Data is collecting, decisions are firing, the dashboard is live. **If you break it, paper trading stops and data collection stops.**

### Rules You Must Follow

**Rule 1 — Never overwrite the file in one shot without a syntax check.**
Always run `python3 -c "import ast; ast.parse(open('autobet_main.py').read()); print('OK')"` before deploying. If it fails, fix the syntax error before copying to the server.

**Rule 2 — Always back up before changing anything.**
Before any session where you modify the file:
```bash
cp ~/autobet/autobet_main.py ~/autobet/autobet_main_backup_$(date +%Y%m%d_%H%M).py
```
There is also a GitHub backup at `https://github.com/crustaison/autobet` (private). Push after every working milestone:
```bash
cd ~/autobet && git add autobet_main.py && git commit -m "description of change" && git push
```

**Rule 3 — Restart the server to test, then check the log.**
```bash
bash ~/autobet/start.sh
sleep 5
curl -s http://localhost:7778/api/health
tail -20 ~/autobet/autobet.log
```
If the log shows a Traceback, fix it before moving on.

**Rule 4 — Do one section at a time.**
Do NOT attempt to implement multiple major sections in the same session. Implement one section, test it works end-to-end, commit to GitHub, then move to the next. The sections are ordered by dependency — do not skip ahead.

**Rule 5 — Never delete working code to replace it with something untested.**
If you are replacing a function, keep the old one renamed (e.g. `_old_build_decisions_page`) until the new one is confirmed working.

**Rule 6 — The database already has data. Do not DROP tables.**
The DB at `~/autobet/data/autobet.db` has ~260,000 kalshi ticks and 652 decisions imported from betbot. Schema migrations must use `ALTER TABLE ADD COLUMN` or `CREATE TABLE IF NOT EXISTS`, never `DROP TABLE` unless you are absolutely certain the table is empty and unused. The `db_migrate()` function already handles known incompatible Clyde-era tables — add to it rather than replacing it.

**Rule 7 — Credentials live in `~/autoresearch/.env`. Never hardcode them.**
Keys: `MINIMAX_API_KEY`, `KALSHI_KEY_ID`. PEM at `~/autoresearch/kalshi.pem`. The `load_env()` function reads these. Don't move them, don't duplicate them, don't print them in logs.

**Rule 8 — Test auth before declaring a page "done".**
Every new page must go through `require_auth()`. Log in with `admin`/`autobet`, confirm the page loads at 200, confirm an unauthenticated request redirects to `/login`.

**Rule 9 — All times display in CT (America/Chicago), not UTC.**
The `now_cst()` and `ts_cst(ts)` helper functions handle this. Never use `datetime.utcnow()` or hardcode UTC-6. The `tz_label()` function returns the correct "CDT"/"CST" string for display.

**Rule 10 — MiniMax responses contain thinking blocks. Always filter them.**
When parsing MiniMax API responses, iterate `resp.get("content", [])` and find the first block where `block.get("type") == "text"`. Never use `resp["content"][0]["text"]` directly — it will KeyError on thinking blocks.

---

## Current State (as of 2026-04-05)

### What is fully working right now:
- Live data collection: Coinbase prices (30s), Kalshi ticks (60s), Polymarket ticks (90s)
- MiniMax M2.5 decisions every 15-min window for BTC/ETH/SOL/XRP
- Paper trading with variable stake sizing (scales by confidence)
- Risk engine: kill switch, daily loss limit, max drawdown %, cooldown after N losses
- Auth: login page, HMAC-SHA256 session cookies, 7-day sessions
- Onboarding wizard: 5-step first-run setup
- Paper runs: isolated experiments per coin, archive/reset
- Historical data: 259,832 Kalshi ticks + 652 decisions imported from betbot
- Pages: Dashboard, Trades, Decisions, Insights, Markets, Runs, Providers, Audit, Settings, Health
- Per-coin drill-down: click any coin card on dashboard → `/coin/BTC` etc.
- Chat popup: floating button on all pages, grounded in DB state
- Tooltips: inline ⓘ hover text
- GitHub backup: `https://github.com/crustaison/autobet` (private)

### What is NOT done (this plan covers these):
1. Decision engine registry (rules engine, KNN, hybrid)
2. Replay mode
3. Live execution (real Kalshi orders)
4. Dataset import wizard
5. Compatibility engine
6. Autoresearch experiment loop
7. Module stubs (oil, sports, elections, mentions)
8. Extended settings UI (model profiles, engine profiles, feature profiles)

---

## Section A — Decision Engine Registry

**Priority: HIGH** — This is the most important remaining feature. Currently MiniMax is hardcoded as the only decision engine. The plan calls for multiple engines that can be selected per market group.

### A.1 What needs to be built

Add a `decision_engines` table (may already exist as Clyde stub — check with `PRAGMA table_info(decision_engines)`). Replace the hardcoded `minimax_analyze()` call in `decision_loop()` with a dispatch to the selected engine for that coin.

### A.2 Engines to implement

**Engine 1: MiniMax LLM (already working)**
- Current behavior: prompt MiniMax M2.5, parse JSON response
- Tag it with `engine_key = "minimax_llm"`
- No changes needed to logic, just register it

**Engine 2: Rules Engine**
- Does NOT require historical data
- Logic:
  ```
  yes_ask = market's yes_ask price
  yes_bid = market's yes_bid price
  mid = (yes_bid + yes_ask) / 2
  spread = yes_ask - yes_bid

  If spread > 0.15: PASS (too wide, bad fill risk)
  If mid > 0.62: YES at yes_ask
  If mid < 0.38: NO at (1 - yes_bid)
  Else: PASS
  ```
- Confidence = distance from 0.5, normalized: `abs(mid - 0.5) * 2`
- This is the safe fallback when MiniMax is unavailable or slow
- Tag: `engine_key = "rules_engine"`

**Engine 3: Vector KNN (requires history)**
- Only usable when `kalshi_ticks` has >= 200 rows for that coin
- Feature vector per window (8 dimensions):
  1. yes_ask at window open
  2. yes_bid at window open
  3. spread (ask - bid)
  4. mid price
  5. coin price delta vs 15 min ago (normalized)
  6. volume proxy (yes_ask_size if available, else 0)
  7. secs_left at observation time
  8. time-of-day fraction (0.0 = midnight, 1.0 = midnight next day)
- Cosine similarity against stored feature vectors for past windows
- Take top 10 nearest neighbors
- Weighted vote by similarity score
- Confidence = weighted win fraction of neighbors
- Entry = mean entry of winning neighbors, clipped to current market bid/ask
- Tag: `engine_key = "vector_knn"`
- If < 200 rows for coin, return PASS with rationale "insufficient history"

**Engine 4: Hybrid (rules gate + KNN)**
- Run rules engine first
- If rules says PASS: return PASS
- If rules says YES or NO: run KNN, take its entry price if available, fall back to rules entry
- Confidence = average of rules confidence and KNN confidence
- Tag: `engine_key = "hybrid"`

### A.3 Database changes needed

```sql
-- decision_engines table (check if exists first)
CREATE TABLE IF NOT EXISTS decision_engines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    engine_key TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    requires_history INTEGER NOT NULL DEFAULT 0,
    min_history_rows INTEGER NOT NULL DEFAULT 0,
    config_json TEXT,
    created_at TEXT
);

-- decision_windows table (feature vectors per window)
CREATE TABLE IF NOT EXISTS decision_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT NOT NULL,
    window_ts INTEGER NOT NULL,
    engine_key TEXT NOT NULL,
    features_json TEXT,
    outcome TEXT,
    created_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dw_coin_wts ON decision_windows(coin, window_ts);

-- market_group_engines: which engine is selected per coin
CREATE TABLE IF NOT EXISTS market_group_engines (
    coin TEXT PRIMARY KEY,
    engine_key TEXT NOT NULL DEFAULT 'minimax_llm',
    updated_at TEXT
);
```

Seed default assignments:
```sql
INSERT OR IGNORE INTO market_group_engines (coin, engine_key) VALUES ('BTC', 'minimax_llm');
INSERT OR IGNORE INTO market_group_engines (coin, engine_key) VALUES ('ETH', 'minimax_llm');
INSERT OR IGNORE INTO market_group_engines (coin, engine_key) VALUES ('SOL', 'minimax_llm');
INSERT OR IGNORE INTO market_group_engines (coin, engine_key) VALUES ('XRP', 'minimax_llm');
```

### A.4 Code changes in decision_loop()

Replace:
```python
result = minimax_analyze(coin, ticks_summary, coin_price)
```
With:
```python
engine_row = conn.execute("SELECT engine_key FROM market_group_engines WHERE coin=?", (coin,)).fetchone()
engine_key = engine_row[0] if engine_row else "minimax_llm"
result = run_engine(engine_key, coin, mkt, ticks, coin_price, ticks_summary)
```

Add `run_engine(engine_key, coin, mkt, ticks, coin_price, ticks_summary)` dispatcher function that calls the right engine and returns `{"direction": ..., "entry": ..., "confidence": ..., "rationale": ...}` or None.

### A.5 Engines page

Add `/engines` to nav. The page shows:
- All registered engines with status (enabled/disabled)
- Per-coin current engine assignment (dropdown to switch)
- History sufficiency indicator per coin per engine (e.g. "vector_knn: 259,832 rows ✓")
- POST `/engines/set` to change a coin's engine

### A.6 Feature vector storage

When the KNN engine runs, store the feature vector in `decision_windows`. This serves double duty: it is the training data for future KNN lookups, and it feeds replay mode. Without storing feature vectors, the KNN engine degrades to a pure rules engine over time.

---

## Section B — Replay Mode

**Priority: HIGH** — Allows honest backtesting on stored history without lookahead.

### B.1 What replay mode must NOT do
- It must never access data that would not have been available at the replay timestamp
- It must never read forward into the dataset past the current replay position
- Results must be stored in a separate `replay_runs` table, never mixed with paper trades

### B.2 Database changes

```sql
CREATE TABLE IF NOT EXISTS replay_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    coin TEXT NOT NULL,
    engine_key TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    starting_capital REAL NOT NULL DEFAULT 500.0,
    status TEXT NOT NULL DEFAULT 'pending',
    config_snapshot TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS replay_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    replay_run_id INTEGER NOT NULL,
    coin TEXT NOT NULL,
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
    engine_key TEXT
);
```

### B.3 Replay engine

A function `run_replay(replay_run_id)` that:
1. Loads the replay run config from DB
2. Iterates through all `window_ts` values in `kalshi_ticks` between `start_ts` and `end_ts` for that coin
3. For each window, builds the context using ONLY ticks with `ts <= window_ts + 180` (first 3 min)
4. Runs the selected engine (rules or KNN — not MiniMax, too slow and wasteful for replay)
5. Determines outcome by looking at price at `window_ts + 900` vs `window_ts`
6. Computes P&L and stores in `replay_trades`
7. Updates `replay_runs.status` from `running` → `complete`

This runs in a background thread so it does not block the HTTP server.

### B.4 Replay page `/replay`

- Form to create a new replay run: coin, engine, date range, starting capital
- Table of existing replay runs with status, P&L, win rate
- Link to drill down into a replay run's trades
- POST `/replay/create` to launch a new replay run
- GET `/replay/{id}` to view a completed run's trades

### B.5 Lookahead protection

When computing the outcome for a replay window, use only:
```python
price_at_open  = get_price_at(conn, coin, window_ts,     tol=120)
price_at_close = get_price_at(conn, coin, window_ts+900, tol=120)
```
Never use the `pm_win_1_signal` or `pm_win_2_signal` columns from the betbot CSV — these are forward-looking signals that would introduce lookahead bias. They exist in the data but must be ignored.

---

## Section C — Live Execution Engine

**Priority: MEDIUM** — Do not attempt this until replay results show consistent positive expected value.

### C.1 Safety gates (ALL must be true to place a live order)

```python
def can_execute_live(coin, direction, entry, size):
    # 1. Global live toggle
    state = conn.execute("SELECT global_live_enabled FROM system_state WHERE id=1").fetchone()
    if not state or not state[0]: return False, "Global live toggle is OFF"

    # 2. Per-coin mode
    mode = settings.get(f"mode_{coin}", "paper")
    if mode != "live": return False, f"{coin} mode is {mode}, not live"

    # 3. Kill switch
    if get_risk_settings()["kill_switch"]: return False, "Kill switch active"

    # 4. Credentials
    if not KALSHI_KEY_ID or not KALSHI_PEM.exists(): return False, "Missing Kalshi credentials"

    # 5. Risk checks
    ok, reason = check_risk(coin, direction, entry, size)
    if not ok: return False, reason

    return True, "ok"
```

### C.2 Order placement via Kalshi API

The Kalshi order endpoint is `POST /trade-api/v2/portfolio/orders`. It requires RSA-PSS authentication (already implemented in `kalshi_auth_headers()`).

Request body:
```json
{
    "ticker": "KXBTC15M-26APR041800-00",
    "action": "buy",
    "side": "yes",
    "type": "limit",
    "count": 10,
    "yes_price": 52
}
```
Note: `yes_price` is in cents (52 = $0.52). `count` is number of contracts. `action` is always `"buy"` for both YES and NO positions (buy YES to bet up, buy NO to bet down).

For a NO bet at `no_price`: set `side = "no"`, `yes_price = 100 - no_price_cents`.

### C.3 Live trade table

Store live orders in a separate table:
```sql
CREATE TABLE IF NOT EXISTS live_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT NOT NULL,
    run_id INTEGER,
    window_ts INTEGER NOT NULL,
    direction TEXT NOT NULL,
    entry REAL NOT NULL,
    size REAL NOT NULL,
    contracts REAL NOT NULL,
    order_id TEXT,
    fill_status TEXT,
    fill_price REAL,
    actual TEXT,
    pnl REAL,
    fee REAL,
    result TEXT,
    coin_open REAL,
    coin_close REAL,
    decided_at TEXT,
    settled_at TEXT,
    error_msg TEXT
);
```

### C.4 Settlement

Live trades must be settled separately from paper trades. Poll `GET /trade-api/v2/portfolio/settlements` every 5 minutes for trades placed in the last 24 hours. Match by `order_id`, update `fill_status`, `actual`, `pnl`, and `result`.

### C.5 IMPORTANT: Do not implement live execution until:
- At least 50 replay windows show positive EV with the selected engine
- At least 2 weeks of paper trading data show win rate > 50%
- Kill switch functionality has been tested (set it, verify no trades fire)
- The `can_execute_live()` gates have been manually verified one by one

---

## Section D — Dataset Import Wizard

**Priority: MEDIUM**

### D.1 What this adds

A UI in Settings > Datasets that lets the operator import CSV or JSON files of historical Kalshi data. The import already works via the "Import from betbot" button. This section formalizes it into a proper wizard.

### D.2 Dataset registry table

```sql
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    coin TEXT,
    source_type TEXT NOT NULL,  -- 'csv', 'json', 'live'
    usage_mode TEXT NOT NULL DEFAULT 'backfill_only',  -- backfill_only, calibration_allowed, compare_only
    row_count INTEGER,
    time_range_start INTEGER,
    time_range_end INTEGER,
    import_notes TEXT,
    created_at TEXT
);
```

### D.3 Import wizard steps

**Step 1 — Upload or specify path**
For LAN use, specifying a file path on ryz.local is fine. No need for browser file upload.
Form field: `source_path` (absolute path on server).

**Step 2 — Preview**
Read first 10 rows, display column names and sample values. Let the operator confirm the mapping:
- `window_ts` → what column holds the window timestamp?
- `yes_bid` / `yes_ask` → which columns?
- `coin_price` → which column?
- Is the timestamp ISO string or Unix integer?
- Are prices in cents (0-100) or decimal (0.0-1.0)?

**Step 3 — Usage mode selection**
- `backfill_only` — fill gaps in live collection history, no calibration use
- `calibration_allowed` — can be used to tune engine thresholds
- `compare_only` — compare against live collected data

**Step 4 — Confirm and import**
Run the import, show progress, store result in `datasets` table.

### D.4 Data lineage

Every decision made using imported data should carry a note in `rationale` that it was trained on imported history. This is already partially done — the betbot import sets `rationale = "betbot import"`.

---

## Section E — Compatibility Engine

**Priority: LOW** — Nice to have, not blocking.

### E.1 Purpose

Prevent the operator from accidentally configuring a combination that cannot work. Examples:
- Selecting `vector_knn` engine for a coin that has < 200 ticks → should show warning, not error
- Selecting `live` mode without global live toggle on → should show clear guidance
- Selecting `hybrid` engine without sufficient history for KNN → should auto-degrade to rules engine

### E.2 Implementation approach

Add a `check_compatibility(coin)` function that returns a list of `{level: "ok"|"warn"|"error", message: "..."}` items.

Call it on:
- The Markets page (show per-coin compatibility status)
- The Engines page (show per-engine readiness)
- Before saving any setting that could create a conflict

### E.3 Rules to implement

```python
COMPAT_RULES = [
    # engine requires history
    {
        "check": lambda coin, settings: (
            settings.get(f"engine_{coin}") in ("vector_knn", "hybrid") and
            get_tick_count(coin) < 200
        ),
        "level": "warn",
        "message": "vector_knn requires >= 200 ticks. Currently using rules_engine fallback."
    },
    # live mode without global toggle
    {
        "check": lambda coin, settings: (
            settings.get(f"mode_{coin}") == "live" and
            not get_global_live_enabled()
        ),
        "level": "error",
        "message": "Coin is in live mode but global live toggle is OFF. No orders will be placed."
    },
    # live mode without credentials
    {
        "check": lambda coin, settings: (
            settings.get(f"mode_{coin}") == "live" and
            (not KALSHI_KEY_ID or not KALSHI_PEM.exists())
        ),
        "level": "error",
        "message": "Live mode requires valid Kalshi credentials."
    },
]
```

---

## Section F — Autoresearch Experiment Loop

**Priority: LOW** — Only implement after live execution is stable.

### F.1 Purpose

A measurable experiment loop that changes one parameter at a time, runs a replay or paper evaluation, scores the result, and keeps or reverts the change.

### F.2 What it is NOT

- It is NOT autonomous. Every change requires operator review before keeping.
- It must NOT modify live settings directly.
- It must NOT run live trades.

### F.3 Experiment types to support

1. **Threshold tuning** — change the rules engine mid/spread thresholds by ±0.02, run replay, compare win rate
2. **Stake policy tuning** — change min/max stake by ±$2, run paper simulation, compare EV
3. **Engine comparison** — run replay with engine A vs engine B on same date range, compare results
4. **Confidence threshold tuning** — if KNN confidence < X, pass. Sweep X from 0.5 to 0.8 in 0.05 steps.

### F.4 Experiment table

```sql
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    param_changed TEXT NOT NULL,
    old_value TEXT NOT NULL,
    new_value TEXT NOT NULL,
    evaluation_type TEXT NOT NULL,  -- 'replay', 'paper'
    baseline_score REAL,
    new_score REAL,
    decision TEXT,  -- 'keep', 'revert', 'pending'
    notes TEXT,
    created_at TEXT,
    decided_at TEXT
);
```

### F.5 Experiment page `/experiments`

- Table of past experiments: parameter changed, old vs new value, score delta, decision
- Button to launch a new experiment (specify parameter, range, evaluation method)
- Clear "Keep" and "Revert" buttons per experiment with confirmation dialogs
- No experiment should auto-apply to live settings without operator click

---

## Section G — Module Stubs

**Priority: LOW** — Placeholder only, no real implementation needed yet.

### G.1 Purpose

The architecture should cleanly support non-crypto modules in the future. This section creates empty stubs so the routing and data model are ready.

### G.2 What to add

A `modules` table with rows for:
- `crypto` (enabled, fully implemented)
- `oil` (disabled, stub only)
- `sports` (disabled, stub only)
- `elections` (disabled, stub only)
- `mentions` (disabled, stub only)

A modules management section in Settings that shows these stubs with "Coming soon" status and a brief description of what each would track.

No actual data collection or decisions needed for stubs.

### G.3 Why this matters

If the modules table and the per-module routing exist from the beginning, adding oil or sports later only requires:
1. Writing the data collection function
2. Writing the feature extraction function
3. Writing the reference authority fetcher
4. Enabling the module in the DB

No rewrites of core routing, risk engine, or dashboard needed.

---

## Section H — Extended Settings UI

**Priority: LOW** — Most settings are already accessible. This adds the remaining MASTER_PLAN items.

### H.1 Model profiles page

A page at `/models` (already has a stub in the old MASTER_PLAN) that shows:
- MiniMax M2.5 (current default) — purpose: decisions
- MiniMax M2.1 (alternative) — purpose: chat (faster for non-decision use)
- MiniMax M2.7 (alternative) — purpose: deep reasoning (slowest)
- Local model endpoint (optional) — `http://localhost:8080/anthropic/v1/messages` format

Lets the operator change which model is used for decisions vs chat without editing the Python file.

Implementation: add `model_profiles` table and read from it in `minimax_analyze()` and `handle_chat()`.

### H.2 Stake policy page

Currently min/max stake is in settings as raw numbers. The MASTER_PLAN calls for named stake policies. Add:
- `stake_policies` table with `name`, `min_stake`, `max_stake`, `confidence_threshold`, `notes`
- A policy picker per coin on the Markets page

For now a single global policy is fine. Named policies become important when you have multiple coins running different strategies.

### H.3 Fee profile versioning

Currently the Kalshi fee rate is hardcoded as `KALSHI_FEE_RATE = 0.07`. Add a `fee_profiles` table with versioned entries so if Kalshi changes their fee structure, historical P&L calculations remain correct for the period they applied.

---

## Build Order for Clyde

Do these in strict order. Do not start Section N+1 until Section N is committed and running.

1. **Section A.1–A.3** (DB schema for engines, seed rows, market_group_engines table)
   - Test: `PRAGMA table_info(decision_engines)` returns rows
   - Commit: `git commit -m "add decision engine schema and seed data"`

2. **Section A.4** (rules engine function + dispatch in decision_loop)
   - Test: switch BTC to `rules_engine` via DB, watch log for `[DECISION] BTC wts=...`
   - Commit: `git commit -m "rules engine + engine dispatch in decision_loop"`

3. **Section A.5** (engines page `/engines`)
   - Test: visit `/engines`, switch a coin to `rules_engine` via dropdown
   - Commit

4. **Section A.6** (vector KNN engine — feature storage first, then KNN lookup)
   - Test: switch BTC to `vector_knn`, confirm decisions fire with `confidence` values
   - Commit

5. **Section B.1–B.3** (replay runs schema + replay engine function)
   - Test: create a replay run via DB insert, run it manually, check `replay_trades` fills
   - Commit

6. **Section B.4** (replay page `/replay`)
   - Test: create replay via UI, watch it complete, check P&L display
   - Commit

7. **Section D** (dataset import wizard in Settings)
   - Test: import a CSV, confirm row count in `datasets` table
   - Commit

8. **Section E** (compatibility engine — just the `check_compatibility()` function and Markets page display)
   - Test: disable global live, set a coin to live mode, confirm warning appears on Markets page
   - Commit

9. **Section G** (module stubs — just the table and settings display)
   - Test: Settings shows Oil/Sports/Elections/Mentions as "coming soon"
   - Commit

10. **Section H.1** (model profiles — table and decision model picker)
    - Test: switch decision model to M2.1, confirm decisions use it
    - Commit

11. **Section C** (LIVE EXECUTION — only after 50+ replay windows show positive EV)
    - Test: set global live toggle ON, set one coin to live mode with very small stake, watch for real orders
    - **CONFIRM WITH SEAN BEFORE ENABLING LIVE ON ANY COIN**
    - Commit

12. **Section F** (autoresearch — only after live is stable for 2 weeks)
    - Commit

---

## File Locations Reference

```
~/autobet/
├── autobet_main.py          # The entire server (~2700 lines, single file)
├── start.sh                 # Restart script
├── logo.jpg                 # Logo served at /logo
├── data/
│   └── autobet.db           # SQLite DB (DO NOT DELETE)
├── MASTER_PLAN_V2.md        # This file
└── OLD_MASTER_PLAN.md       # Original Clyde/Claude spec

~/autoresearch/
├── .env                     # Credentials (MINIMAX_API_KEY, KALSHI_KEY_ID)
├── kalshi.pem               # RSA private key for Kalshi
└── data/                    # Betbot historical data (already imported)
    ├── kalshi_ticks.csv      # BTC (80k rows)
    ├── kalshi_eth_ticks.csv  # ETH (72k rows)
    ├── kalshi_sol_ticks.csv  # SOL (51k rows)
    ├── kalshi_xrp_ticks.csv  # XRP (53k rows)
    ├── kalshi_decisions.json # BTC decisions (215)
    └── kalshi_decisions_*.json # ETH/SOL/XRP decisions
```

## Quick Reference Commands

```bash
# Restart server
bash ~/autobet/start.sh

# Check it's running
curl -s http://localhost:7778/api/health

# Tail log
tail -50 ~/autobet/autobet.log

# Syntax check before deploy
python3 -c "import ast; ast.parse(open('autobet_main.py').read()); print('OK')"

# GitHub backup
cd ~/autobet && git add autobet_main.py && git commit -m "description" && git push

# Manual DB check
python3 -c "import sqlite3; c=sqlite3.connect('data/autobet.db'); print([r[0] for r in c.execute('SELECT name FROM sqlite_master WHERE type=\"table\"').fetchall()])"

# Current paper account state
python3 -c "import sqlite3; c=sqlite3.connect('data/autobet.db'); [print(dict(r)) for r in c.execute('SELECT coin,capital,wins,losses,total_pnl FROM paper_accounts').fetchall()]"

# Count ticks per coin
python3 -c "import sqlite3; c=sqlite3.connect('data/autobet.db'); [print(r) for r in c.execute('SELECT coin, COUNT(*) FROM kalshi_ticks GROUP BY coin').fetchall()]"
```

---

## Final Note

This is a living document. Update it as sections are completed. Check off items here and commit this file to GitHub with each milestone. If you finish a section and find the design needed to change, document what changed and why at the bottom of that section — future maintainers (and Clyde) will thank you.

The most important thing: **keep the server running**. Data collection must not be interrupted. Every hour of Kalshi ticks is valuable training data. If you are uncertain about a change, back up first, test on a copy, and only deploy when confident.
