# AUTOBET - Modular Prediction Market Platform

## Status: BUILDING

## Build Order (per master plan section 38)
1. ✅ Create repo structure
2. ⏳ Create SQLite schema and migrations  
3. ⏳ Build Go HTTP server
... (see internal/ for full build)

## Quick Start
```bash
cd autobet
go build -o bin/server ./cmd/server
./bin/server
```

## Access
- Dashboard: http://localhost:7778 (or LAN IP:7778)

## Tech Stack (per master plan section 4)
- Backend: Go
- Research: Python  
- Database: SQLite
- Frontend: SvelteKit

## Modules (per master plan section 23)
- Crypto: FIRST - building now
- Oil: placeholder
- Sports: placeholder
- Elections: placeholder
- Mentions: placeholder

## Execution Modes (per master plan section 3.5-3.7)
- disabled: no data collection
- observe: collect data, generate signals, no trades
- paper: simulate trades, no real orders
- live: real orders if safety checks pass