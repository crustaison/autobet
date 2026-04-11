# autobet

Multi-venue prediction market trading platform. Trades Kalshi 15-minute crypto contracts across BTC, XRP, SOL, ETH, DOGE, BNB, HYPE. Supports paper and live trading with dual-LLM decision making (MiniMax M2.5 primary + adversarial skeptic), per-coin engine selection, Kelly-criterion sizing, smart money copy-trading, pre-signal fast entry, reversal hedging, lottery buys, and full audit trail.

**Dashboard:** http://ryz.local:7778

**Overall paper performance (625 trades):** 51.7% WR · +$73,053 P&L

---

## Stack

- Single Python file: `autobet_main.py` (~5,000 lines)
- SQLite database: `data/autobet.db` (WAL mode)
- Stdlib HTTP server — no external web frameworks
- Background threads: prices (30s), Kalshi ticks (adaptive: 10s active / sleep-to-boundary idle), Polymarket + wallet signals (90s), decision loop, pre-signal (fires 65s before each window), live order sync (60s)

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
| `TRADE_SIZE` | $15 | Default stake per trade |

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
12. `check_lottery_buys()` runs alongside exit check — fires lottery orders in last 5 min if cheap contracts appear
12. `resolve_live_orders()` fetches Kalshi settlement after window close and records P&L

---

## Early Exit Engine

Monitors all filled live positions every 60 seconds. Five exit rules (checked in priority order):

| Rule | Trigger | Setting key |
|---|---|---|
| **Price target** | Contract price hits ≥ N¢ | `exit_price_target_cents` (default 90¢) |
| **Trailing stop** | Was up ≥ take_profit_pct%, then fell 15% from peak | `exit_take_profit_pct` (default 40%) |
| **Profit lock** | Up ≥ take_profit_pct% with < 120s remaining | `exit_take_profit_pct` |
| **Stop loss** | Down ≥ stop_loss_pct% of stake | `exit_stop_loss_pct` (default 65%) |
| **Time cliff** | Any profit with ≤ N seconds remaining | `exit_time_cliff_secs` (default 90s) |
| **LLM check** | At ~midpoint (~450s left), asks LLM hold/sell if P&L > 5% | `exit_llm_check` (default on) |

Peak unrealized P&L is tracked per-position in settings (`trailing_peak_{order_id}`). Sells via `"action": "sell"` on the Kalshi orders endpoint.

### Reversal Hedge

When a position exits at ≥ `exit_price_target_cents`, the bot immediately buys `hedge_contracts` (default 25) of the **opposite side** at a limit of `hedge_max_ask_cents` (default 3¢).

- **Cost:** $0.25 per position exit (25 × 1¢)
- **Upside:** If market reverses after exit, 25 × $1 = $25 from a $0.25 bet
- **Held to expiry** — not subject to stop-loss, trailing stop, or time cliff
- Configurable: `hedge_enabled`, `hedge_contracts`, `hedge_max_ask_cents`

### Lottery Buys

Independent of existing positions. In the last 1–5 minutes of any window (`lottery_min_secs_left`–`lottery_max_secs_left`), if either YES or NO drops to ≤ `lottery_max_ask_cents` (default 2¢), the bot buys `lottery_contracts` (default 20) contracts at market.

- **Cost:** $0.20 per trigger (20 × 1¢)
- **Upside:** 20 × $1 = $20 from a $0.20 bet if the market snaps back
- Fires once per coin per window, held to expiry
- Configurable: `lottery_enabled`, `lottery_contracts`, `lottery_max_ask_cents`, `lottery_min_secs_left`, `lottery_max_secs_left`

---

## Pre-Signal Fast Entry

A dedicated `pre_signal_loop` thread fires MiniMax analysis for all live coins **~65 seconds before each window boundary**. The result is cached in `_pre_signals`.

At window open, `_decide_coin` checks for a cached pre-signal (< 120s old). If found:
- Skips the MiniMax API call entirely
- Uses the cached direction/confidence
- Recalculates entry from the live market price at window open
- Places the order within **2–3 seconds** of window open instead of 15–20s

Without pre-signal: order arrives 15–20s into the window (market may have moved from 19¢ to 40¢). With pre-signal: order arrives at 2–3s, capturing the opening price.

Pre-signals are consumed on use. If the pre-signal is stale or absent, the bot falls back to the normal MiniMax call.

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

---

## Requirements

### Hardware

Autobet runs comfortably on any always-on Linux machine. The current deployment is an **AMD Ryzen 9 6900HX mini-PC** with 60GB RAM, but the actual footprint is minimal:

| Resource | Minimum | Recommended | Notes |
|---|---|---|---|
| CPU | Any modern x86_64 or ARM64 | 4+ cores | Decision loop + background threads are light |
| RAM | 512MB free | 2GB+ | SQLite + Python process; more = headroom for local LLM |
| Storage | 1GB | 10GB+ | DB grows ~50MB/month at full trading cadence |
| Network | Stable broadband | Wired/low-latency | Kalshi API calls are time-sensitive; flaky connections miss windows |
| OS | Linux (Ubuntu 22.04+, Debian 12+) | Same | Tested on x86_64 and ARM64 |

**Always-on requirement:** autobet must run continuously to catch every 15-minute window. A desktop you sleep or a laptop you close will miss trades. Use a mini-PC, server, or VPS.

### Python

- **Python 3.10+** (3.11+ recommended)
- One non-stdlib dependency: `cryptography` (Kalshi RSA request signing)

```bash
pip install cryptography
```

### Accounts & API Keys

| Service | Purpose | Required for | Cost |
|---|---|---|---|
| **Kalshi** | Prediction market exchange — placing and resolving trades | Live trading | Free account; funded balance needed for live mode |
| **MiniMax** | AI decision engine (dual-LLM calls per window) | `minimax_llm` engine (default) | Pay-per-token; ~$0.002–0.005 per decision window |
| **Polymarket** | Cross-venue price signal + smart money copy-trading | Signal enrichment | No account needed — public API only |

#### Kalshi Setup
1. Create account at kalshi.com
2. Fund your account ($20+ for testing; $100+ for live trading)
3. Generate an API key: Account → API → Create Key
4. Download the RSA private key (.pem)
5. Add to `~/autoresearch/.env`:
   ```
   KALSHI_KEY_ID=your-key-id-here
   ```
6. Place private key at `~/autobet/kalshi.key`

#### MiniMax Setup
1. Create account at minimax.io
2. Generate an API key
3. Add to `~/autoresearch/.env`:
   ```
   MINIMAX_API_KEY=your-key-here
   ```

**No MiniMax?** Switch coins to `rules_engine` or `hybrid` on the Markets page — zero API calls. Or pipe any local LLM (Ollama, llama.cpp) into the `betbot_signal` JSON format.

### Local Model Option (no cloud AI cost)

Any local LLM can replace MiniMax via the `betbot_signal` engine by writing decisions to `~/autoresearch/data/kalshi_signals*.json`. Minimum hardware for useful local inference:

| Setup | Latency | Notes |
|---|---|---|
| 7B model, CPU only | 10–30s | Usable — decision window is 3 min |
| 7B model, GPU 8GB VRAM | 2–5s | Comfortable |
| 30B+ model, NPU/GPU | <5s | Current ryz.local setup (Nexa SDK + Vulkan) |

A 7B+ model is the practical minimum — smaller models produce inconsistent JSON output.

### Capital

| Mode | Minimum | Notes |
|---|---|---|
| Paper trading | $0 | Simulated against real market data, no real money |
| Live trading | ~$50 | Practical floor given $10 min stake and fees |
| Live + Pool mode | $200+ | Enough to trade multiple windows without balance risk |
| Per-coin allocation *(future)* | $400+ | Virtual per-coin budgets within one Kalshi account |

Run paper mode until win rate is consistently above 52% over 100+ trades before going live.

---

## AI Chat Assistant

A floating chat button (bottom-right on every page) gives you a conversational interface to the platform.

### How It Works

1. You type a question in the chat panel
2. The server fetches live evidence from the DB: last 10 decisions (coin, direction, confidence, result, P&L, rationale), all paper account balances, current prices, and risk settings
3. All of that context is injected into a prompt sent to **MiniMax** via the Anthropic-compatible API
4. The reply is displayed in the chat panel and persisted in `localStorage` so history survives page reloads

### What You Can Ask

- **Performance questions** — "Why is SOL losing?" / "What was the last BTC decision?"
- **Feature explanations** — "What does the entry ceiling do?" / "How does pool mode work?"
- **Current state** — "What are the current paper balances?" / "Is the kill switch on?"
- **Strategy questions** — "What does ex-outlier P&L mean?" / "When does the rules engine fire?"

### Grounding

The chat is grounded in real DB state — it sees actual recent decisions and balances, not generic knowledge. It will not hallucinate trade history it cannot see in the evidence snapshot.

### Requirements

- MiniMax API key must be set (`MINIMAX_API_KEY` in `~/autoresearch/.env`)
- Without a key, the chat returns: *"MiniMax API key not configured."*
- If the session expires, the chat returns a 401 and prompts you to reload — session cookies persist across server restarts

### Chat Controls

| Action | How |
|---|---|
| Send message | Type + Enter |
| Clear history | Click **Clear** button in chat panel |
| History persistence | Stored in browser `localStorage` — survives page reloads, cleared on explicit Clear |
