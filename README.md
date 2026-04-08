# autobet

Multi-venue prediction market trading platform. Trades Kalshi 15-minute crypto contracts across BTC, XRP, SOL, ETH, DOGE, BNB, HYPE. Supports paper and live trading modes with per-coin engine selection, risk controls, pool mode, and full audit trail.

**Dashboard:** http://ryz.local:7778

## Stack

- Single Python file: `autobet_main.py` (~4,500 lines)
- SQLite database: `data/autobet.db`
- Background threads: price collection (30s), Kalshi ticks (60s), Polymarket (90s), decision loop, live order sync (60s)
- No frameworks — stdlib HTTP server

## Start / Restart

```bash
bash ~/autobet/start.sh
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

## Signal Quality Filters

Applied per-window before any trade is placed (non-betbot decisions only):

| Filter | Default | Setting key |
|---|---|---|
| Min confidence | 0.55 | `min_confidence` in settings table |
| Min market volume | 500 contracts | `min_volume` in settings table |
| Entry ceiling | 0.80 | `ENTRY_CEILING` constant |
| SOL YES entry cap | 0.55 | Hardcoded — SOL YES above 0.55 is 36-43% WR historically |

## Decision Engines

| Engine | Description |
|---|---|
| **minimax_llm** | MiniMax M2.1 API call per window with tick history + coin price (~2-5s) |
| **rules_engine** | Kalshi mid > 0.62 → YES, mid < 0.38 → NO, else PASS. Zero API calls. |
| **vector_knn** | 8-feature cosine similarity against resolved historical windows. Needs 20+ resolved trades. |
| **hybrid** | Rules gate first, then KNN confidence boost if same direction. |
| **betbot_signal** | Reads `~/autoresearch/data/kalshi_signals*.json` written by betbot's autoresearch loop. |

Engine is configurable per-coin from the Markets page. Default: `minimax_llm`.

## Live Order Flow

1. Decision loop fires in first 3 minutes of each 15-min window
2. Engine produces direction/entry/confidence
3. Signal quality filters applied (confidence, volume, entry ceiling, bias filters)
4. Risk engine checks (kill switch, daily loss limit, drawdown, cooldown)
5. Liquidity check against live Kalshi order book
6. Paper trade recorded; if coin is in live mode, order placed via Kalshi REST API
7. `sync_live_orders()` polls Kalshi every 60s to update fill status
8. `resolve_live_orders()` fetches market settlement after window close and records actual P&L

## Pool Mode

When enabled, all live-mode coins compete each window. Each coin's engine runs in parallel, signals are scored by `confidence × 0.7 + rolling_win_rate × 0.3`, and only the top scorer places a real order. Prevents multiple simultaneous live positions.

## Risk Controls

Configurable from the Settings page:

- **Kill switch** — halts all trading immediately
- **Daily loss limit** — per-coin, resets at midnight CT
- **Max drawdown %** — stops trading if capital falls below % of starting balance
- **Max stake** — hard cap per trade
- **Cooldown after N losses** — pauses coin after N consecutive losses

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

## Database Tables

`users`, `system_state`, `price_history`, `kalshi_ticks`, `polymarket_ticks`, `paper_runs`, `paper_accounts`, `paper_trades`, `live_orders`, `decisions`, `risk_settings`, `audit_logs`, `settings`, `fill_quality`, `coin_modes`, `market_group_engines`, `replay_runs`, `replay_trades`, `import_jobs`

## Syntax Check

```bash
python3 -c "import ast; ast.parse(open('autobet_main.py').read()); print('OK')"
```
