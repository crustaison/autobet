# autobet

Multi-venue prediction market paper trading platform. Tracks Kalshi 15-minute crypto contracts across BTC, XRP, SOL, ETH. Supports paper and live trading modes.

**Dashboard:** http://ryz.local:7778

## Stack

- Single Python file: `autobet_main.py`
- SQLite database: `data/autobet.db`
- Background threads: price collection, Kalshi ticks, Polymarket, decision loop, live order sync
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

- `~/autoresearch/.env` — `MINIMAX_API_KEY`, `KALSHI_KEY_ID`, `COINBASE_KEY_NAME`
- `~/autobet/kalshi.key` — RSA private key for Kalshi API auth

## Key Constants

| Constant | Value |
|---|---|
| `ENTRY_FLOOR` | 0.05 |
| `ENTRY_CEILING` | 0.95 |
| `MAX_CONTRACTS` | 500 |
| `KALSHI_FEE_RATE` | 7% of profit, capped $0.02/contract |

## Decision Engines

- **minimax_llm** — MiniMax M2.5 API call per window (~2-5s)
- **rules_engine** — Kalshi mid > 0.62 YES / < 0.38 NO threshold
- **vector_knn** — 10-nearest historical windows, cosine similarity
- **hybrid** — rules gate + KNN confirmation
- **betbot_signal** — reads `~/autoresearch/data/kalshi_signals*.json`

## Pages

Dashboard, Trades, Decisions, Markets, Runs, Fill Quality, Providers, Audit, Settings, Health, Import, Research, Chat

## Syntax check

```bash
python3 -c "import ast; ast.parse(open('autobet_main.py').read()); print('OK')"
```
