# AUTOBET TODO List
Updated: April 6, 2026 (After Analysis)

## Current Status: RUNNING + PROFITABLE
- 287 trades today, $70,796 P&L
- BTC: $50K, ETH: $18K, SOL: $1.4K, XRP: $620

## Priority 1: Critical (Before Live Trading)
- [X] Hard ban entries > 0.95 (both YES and NO) - ENTRY_CEILING=0.95 active in code
- [ ] REAL liquidity check - fetch kalshi order book API, check actual bid qty before trade
- [ ] Implement order book fill tracking table (fill_quality)
- [ ] Add slippage tracking per price range
- [ ] Automated size reduction when liquidity insufficient
- [ ] Implement order status monitoring (open, filled, cancelled, expired)
- [x] Fix fee calculation (exact formula)
- [ ] Disable fallback logic

## Priority 2: High
- [ ] Add per-coin min_stake and max_stake (different ranges for BTC/ETH/SOL/XRP)
- [ ] Add rolling win-rate per coin to decision engine
- [ ] Flag "too good to be true" entries (price < 0.01)
- [x] Fix Settings UI step="5" -> step="1" (in autobet_main.py code, needs server restart)
- [ ] Cap notional exposure

## Priority 3: Medium-Term
- [ ] FIX: Tooltips not working on Insights page (CONFIDENCE CALIBRATION, ENTRY PRICE VS EDGE, etc.) - only works in full autobet_main.py UI, not simple server.py
- [ ] Fix tooltip/popup positioning - mouseovers going off-screen, need to center on screen
- [ ] Add WHY column tooltip on Decisions page - text is truncated, needs hover popup to show full rationale
- [ ] Add sortable columns to all table views (click header to sort Asc/Desc)
- [ ] Engine Manager - add descriptions for each engine (minimax_llm, rules, knn, hybrid) visible in UI, not just tooltips
- [ ] FIX Providers page - show which coins are mapped to each provider, allow adding/editing provider config
- [ ] FIX Research/Autoresearch page - model detection not working (MiniMax M2.7)
- [ ] FIX Research page - show all coins, not just BTC (add selector for ETH/SOL/XRP)
- [ ] FIX Research page - add "Copy Code" button for code blocks
- [ ] Add /export page ( mirroring /import for data export)
- [ ] Realistic fill simulator
- [ ] UI contrast fix - text on cards is hard to read, bars need better visibility (contrast issues)
- [ ] FIX AI Chat - resets on page refresh, should retain 20 responses of conversation history
- [ ] Add Admin user management - create/delete users, role permissions (RBAC)
- [ ] Recalibrate confidence scoring
- [ ] Add order book depth to AI

## Priority 4: Research
- [ ] Separate YES vs NO performance
- [ ] Validate entry bands

## DONE / Working
- [X] Decision engines (minimax, rules, knn, hybrid)
- [X] Cooldown mechanism working
- [X] Live prices from Coinbase
- [X] Paper trading system
- [X] Fee overcharge identified (~$2-5K across session)
