# autobet

Multi-venue prediction market trading platform. Trades Kalshi 15-minute crypto contracts across BTC, XRP, SOL, ETH, DOGE, BNB, HYPE. Supports paper and live trading with dual-LLM decision making (MiniMax M2.5 primary + adversarial skeptic), per-coin engine selection, Kelly-criterion sizing, smart money copy-trading, early exit engine, and full audit trail.

**Dashboard:** http://ryz.local:7778

**Overall paper performance (625 trades):** 51.7% WR · +$73,053 P&L

---

## Stack

- Single Python file: `autobet_main.py` (~5,000 lines)
- SQLite database: `data/autobet.db` (WAL mode)
- Stdlib HTTP server — no external web frameworks
- Background threads: prices (30s), Kalshi ticks (60s), Polymarket + wallet signals (90s), decision loop, live order sync (60s)

## Start / Restart

```bash
bash ~/autobet/start.sh
```

Systemd service (auto-restart on crash):
```bash
sudo systemctl start autobet
sudo systemctl status autobet
journalctl -u autobet -f
```

Or manually:
```bash
pkill -f 'python3 autobet_main.py'
fuser -k 7778/tcp
sleep 2
cd ~/autobet && nohup python3 -u autobet_main.py >> autobet.log 2>&1 &
```

## Credentials

- `~/autoresearch/.env` — `MINIMAX_API_KEY`, `KALSHI_KEY_ID`
- `~/autobet/kalshi.key` — RSA private key for Kalshi API (PSS/SHA256)

## Key Constants

| Constant | Value | Notes |
|---|---|---|
| `ENTRY_FLOOR` | 0.05 | Below this = unrealistic order book depth |
| `ENTRY_CEILING` | 0.80 | Above this = confirmed negative EV (entry 0.8+ = -$1,886 in live data) |
| `MAX_CONTRACTS` | 500 | Order book depth cap per trade |
| `KALSHI_FEE_RATE` | 7% | Of `entry × (1-entry)`, capped at $0.02/contract |
| `STARTING_CAPITAL` | $500 | Per-coin paper account starting balance |
| `TRADE_SIZE` | $20 | Default stake per trade |

---

## Decision Engines

| Engine | Description |
|---|---|
| **minimax_llm** | Dual-LLM: two parallel MiniMax M2.5 calls — primary synthesizer + adversarial skeptic. Results reconciled — agreement boosts confidence 8%, disagreement penalizes 35%. All sub-engines feed context into both prompts. ~3-5s per call. |
| **rules_engine** | Kalshi mid > 0.62 → YES, mid < 0.38 → NO, else PASS. Zero API calls. |
| **vector_knn** | 8-feature cosine similarity against resolved historical windows (needs 20+ resolved trades). |
| **hybrid** | Rules gate first, then KNN confidence boost if same direction. |
| **betbot_signal** | Reads `~/autoresearch/data/kalshi_signals*.json` written by betbot's autoresearch loop. |

Engine is configurable per-coin from the Markets page. Default: `minimax_llm`.

### Dual MiniMax M2.5 Architecture

Two independent MiniMax M2.5 calls run in parallel every decision window:

| Call | Role | Temperature | Framing |
|---|---|---|---|
| **Primary** | Synthesize all signals into a decision | Default | "You are the final decision-maker — synthesize all signals" |
| **Skeptic** | Challenge the consensus | 0.4 | "Assume the obvious direction is wrong — find the counter-argument" |

**Reconciliation:**
- Both agree → confidence boosted: `(avg_conf × 1.08)`, rationale tagged `[skeptic agrees ↑]`
- Disagree → confidence penalized: `primary_conf × 0.65`, rationale tagged `[skeptic disagrees ↓]`
- One fails/times out → other's result used alone

### What the LLM sees per window

The prompt fed to both LLMs includes:

- Current coin spot price
- 24h market volume and bid-ask spread
- Last 5-minute order book tick history (yes_bid / yes_ask snapshots)
- Rules engine signal (direction, entry, confidence, zone)
- KNN signal (direction, entry, confidence, k-nearest rationale)
- Price momentum (% change over last 5 min — bullish/bearish/flat)
- Polymarket cross-venue YES price (arb gap detection)
- Last 3 resolved window outcomes for this coin (WIN/WIN/LOSS sequence)
- Fee-adjusted entry cost (raw entry + 7% fee per contract)
- Order book depth ladder (top 3 YES/NO ask levels with qty)
- Kalshi market comments (recent trader discussion, scraped every 5 min)
- Smart money signal (top leaderboard wallet activity — see Copy-Trading)

### Auto-Switch Engine

The LLM can suggest switching to a better engine via `suggest_engine` in its JSON output. After **8 consecutive windows** of the same suggestion, the engine switches automatically. If the new engine's win rate drops below **38%** over 10 trades, it reverts to `minimax_llm`.

---

## Bet Sizing — Kelly Criterion

Stake is calculated using half-Kelly: `f = (p - e) / (1 - e) × 0.5 × capital`

- `p` = confidence (0–1)
- `e` = entry price
- Capped at `max_stake` and 10% of coin capital
- `max_stake` scales proportionally with capital growth above `STARTING_CAPITAL` — compounding is automatic

---

## Signal Quality Filters

Applied per-window before any trade is placed:

| Filter | Default | Setting key |
|---|---|---|
| Min confidence | 0.55 | `min_confidence` |
| Min market volume | 500 contracts | `min_volume` |
| Entry ceiling | 0.80 | `ENTRY_CEILING` constant |
| SOL YES entry cap | 0.55 | Hardcoded — 36-43% WR above this historically |
| Hour blackout | 8,10,11,17,18,23 CT | `blackout_hours` (data-driven from WR by hour) |
| Coin auto-pause | WR < 42% over last 15 trades | `autopause_wr_threshold` |
| Window entry timing | Skip if < 60s into window | Hardcoded — avoids thin early liquidity |
| Late entry block | Skip if < 120s remaining | Hardcoded — avoids bad fills at window close |

---

## Live Order Flow

1. Decision loop fires in first 3 minutes of each 15-min window
2. Timing guard: waits until ≥60s into window (liquidity settles)
3. Hour blackout and coin auto-pause filters checked
4. Volume, confidence, entry ceiling, SOL bias filters applied
5. Polymarket signal, wallet copy signals, comments, order book depth, fee note gathered
6. Engine runs (all sub-signals pre-computed and injected into LLM prompt)
7. Risk engine checks (kill switch, daily loss limit, drawdown, cooldown)
8. Live liquidity check — reduces contracts to available depth, blocks if < 10 contracts
9. Paper trade recorded; if coin is live mode, order placed via Kalshi REST API
10. `sync_live_orders()` polls Kalshi every 60s to update fill status
11. `check_exit_positions()` evaluates early exit every 60s (see Early Exit Engine)
12. `resolve_live_orders()` fetches Kalshi settlement after window close and records P&L

---

## Early Exit Engine

Monitors all filled live positions every 60 seconds. Four exit rules:

| Rule | Trigger | Setting key |
|---|---|---|
| **Trailing stop** | Was up ≥ take_profit_pct%, then fell 15% from peak | `exit_take_profit_pct` (default 40%) |
| **Profit lock** | Up ≥ take_profit_pct% with < 120s remaining | `exit_take_profit_pct` |
| **Stop loss** | Down ≥ stop_loss_pct% of stake | `exit_stop_loss_pct` (default 65%) |
| **Time cliff** | Any profit with ≤ N seconds remaining | `exit_time_cliff_secs` (default 90s) |
| **LLM check** | At ~midpoint (~450s left), asks LLM hold/sell if P&L > 5% | `exit_llm_check` (default on) |

Peak unrealized P&L is tracked per-position in settings (`trailing_peak_{order_id}`). Sells via `"action": "sell"` on the Kalshi orders endpoint.

---

## Pool Mode

When enabled, all live-mode coins run their engines in parallel each window. Signals are scored:

```
score = confidence × 0.7 + rolling_win_rate × 0.3
```

**Correlated coin deduplication**: Only 1 position allowed per correlation group:
- Group A: `BTC`, `ETH`
- Group B: `SOL`, `XRP`, `DOGE`, `BNB`, `HYPE`

**Multi-position threshold** (`pool_multi_threshold`): If set > 0 (e.g. 0.65), places orders for all coins scoring above the threshold rather than just the single winner. Default 0 = single winner only.

---

## Polymarket Copy-Trading (Smart Money)

Automatically tracks and signals what top Polymarket leaderboard traders are buying.

### Auto-Discovery (runs every 24 hours)
1. Scrapes `polymarket.com/leaderboard/crypto/weekly/profit` — finds all wallet addresses
2. Tests each against the Polymarket activity API
3. Keeps wallets with ≥3 recent crypto trades (active in our markets)
4. Saves top 25 wallets to `poly_tracked_wallets` setting
5. Seeded with 4 known top performers (#1 +$723k, #5 +$412k, #7 +$379k, #12 +$225k)

### Signal Polling (every 90 seconds)
- Checks last 20 minutes of activity for all tracked wallets
- Aggregates YES/NO buy counts + dollar volume per coin
- LLM prompt receives: `"Smart money (top leaderboard wallets, last 20min): 3/5 bought YES, 2/5 bought NO $847 total volume → YES lean"`

Manually override wallet list via Settings → "Tracked Polymarket Wallets" (comma-separated addresses).

---

## Risk Controls

Configurable from the Settings page:

| Control | Description |
|---|---|
| **Kill switch** | Halts all trading immediately |
| **Daily loss limit** | Per-coin, resets at midnight CT |
| **Max drawdown %** | Stops coin if capital falls below % of starting balance |
| **Max stake** | Hard cap per trade (scales with capital compounding) |
| **Cooldown after N losses** | Pauses coin after N consecutive losses |
| **Coin auto-pause** | Pauses coin if rolling WR (last 15 trades) < threshold |
| **Hour blackout** | Skips trading during specified CT hours (default: hours with negative historical P&L) |

---

## Pages

| Page | Path |
|---|---|
| Dashboard | `/` |
| Trades | `/trades` |
| Decisions | `/decisions` |
| Markets | `/markets` |
| Runs | `/runs` |
| Perf / Insights | `/perf` |
| Fill Quality | `/fill-quality` |
| Providers | `/providers` |
| Audit | `/audit` |
| Settings | `/settings` |
| Health | `/health` |
| Import | `/import` |
| Research / Replay | `/research` |
| Chat | floating button |

---

## Settings Reference

| Key | Default | Description |
|---|---|---|
| `min_confidence` | 0.55 | Skip trades below this confidence |
| `min_volume` | 500 | Skip windows below this 24h contract volume |
| `blackout_hours` | 8,10,11,17,18,23 | CT hours to skip (comma-separated) |
| `autopause_wr_threshold` | 0.42 | Auto-pause coin if rolling WR falls below this |
| `exit_take_profit_pct` | 40 | Trailing stop target % of stake |
| `exit_stop_loss_pct` | 65 | Stop loss % of stake |
| `exit_time_cliff_secs` | 90 | Exit any winning position with ≤ N seconds left |
| `exit_llm_check` | 1 | LLM hold/sell check at window midpoint |
| `pool_multi_threshold` | 0 | Pool: place all coins scoring ≥ this (0 = winner only) |
| `poly_tracked_wallets` | (auto) | Comma-separated Polymarket wallet addresses |
| `min_stake` | $10 | Minimum stake per trade |
| `max_stake` | $30 | Maximum stake per trade (scales with capital growth) |

---

## Database Tables

`users`, `system_state`, `price_history`, `kalshi_ticks`, `polymarket_ticks`, `paper_runs`, `paper_accounts`, `paper_trades`, `live_orders`, `decisions`, `risk_settings`, `audit_logs`, `settings`, `fill_quality`, `coin_modes`, `market_group_engines`, `replay_runs`, `replay_trades`, `import_jobs`

---

## Syntax Check

```bash
python3 -c "import ast; ast.parse(open('autobet_main.py').read()); print('OK')"
```


## Win Rate by Hour (CT) — basis for default blackout hours

| Hour | WR% | P&L |
|---|---|---|
| 0:00 | 68% | +$1,509 |
| 1:00 | 59% | +$737 |
| 6:00 | 58% | +$37,532 |
| 7:00 | 58% | +$4,529 |
| 9:00 | 54% | +$13,271 |
| 19:00 | 68% | +$13,968 |
| 22:00 | 79% | +$227 |
| **8:00** | **33%** | **-$444** |
| **10:00** | **42%** | **-$281** |
| **11:00** | **43%** | **-$315** |
| **17:00** | **24%** | **-$226** |
| **18:00** | **38%** | **-$207** |
| **23:00** | **39%** | **-$32** |

Default blackout covers all net-negative hours. Adjustable in Settings.
