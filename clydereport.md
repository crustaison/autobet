# Clyde's AUTOBET Analysis Report
Date: April 6, 2026
Author: Clyde (AI Analysis)

---

## Executive Summary

The trading system executed 109 trades across 4 coins (BTC, XRP, SOL, ETH) during an 8-hour period on April 6, 2026. Overall net P&L: ~$47,700 with a 52% win rate.

However, the results are heavily driven by outlier trades. Without the top 3 wins, the system would have lost money.

---

## 1. Key Findings

### 1.1 Outlier Dependence
- Top 3 wins = ~$47,000 of the ~$47,700 total
- Without outliers: small loss
- **Bottom line:** Strategy is narrowly profitable, not broadly robust

### 1.2 Entry Price Patterns

| Band | Performance | Verdict |
|------|-------------|---------|
| < 0.01 (cheap) | Huge wins | Real edge signal, but liquidity issue |
| 0.01 - 0.95 (mid) | Break-even | Noise |
| > 0.95 (expensive) | Consistently lose | Value destructive |

### 1.3 Coin Rankings

1. **BTC** — Highest win rate (67%), but most outlier-dependent
2. **XRP** — Most balanced, best candidate for refinement
3. **SOL** — NO-collapse shows promise, otherwise mixed
4. **ETH** — Weakest, cooldown is the bright spot

---

## 2. Technical Issues to Fix

### 2.1 Fee Calculation (OVERCHARGING)

**Current model:**
```python
fee = min(0.07 * abs(profit), 0.02 * contracts)
```

**Issue:** For trades with entry < $0.01, calculates fee on profit (tiny), NOT using per-contract formula. This OVERCHARGES fees.

**Example Trade 168:**
- Entry: $0.001, 38,000 contracts
- Current fee: $760
- Correct fee: $2.66 (using exact formula)
- Overcharge: ~$757

**Exact formula (Kalshi official):**
```python
per_contract_fee = 0.07 * entry_price * (1 - entry_price)
fee = min(per_contract_fee, 0.02) * contracts
```

**Impact:** Current model overestimates fees by 10-100x on cheap entries, underestimates slightly on mid-range. Total overcharge across session: ~$2,000-5,000.

**FIX LATER**

---

### 2.2 Liquidity Assumption (UNREALISTIC)

- Trade 168: Bought 38,000 contracts at $0.001
- Reality: Only ~100-500 would fill at that price
- Remaining would slip to $0.01-0.05
- P&L overstatement: **18-74x**

This applies to ALL cheap-entry trades (< $0.01).

**FIX LATER:** Add ask_size check, realistic fill simulator

---

### 2.3 Confidence Calibration (NOISY)

- 85% confidence trades still losing regularly
- Current confidence = ranking label, NOT probability
- Cannot use for aggressive sizing

**No fix planned yet**

---

### 2.4 Fallback Logic (WEAK)

- "Market favors YES/NO" fallback goes 0-5 on BTC
- Should be disabled or isolated

**Recommend:** Disable fallback in production

---

## 3. Risk Controls (WORKING)

### 3.1 Cooldown Mechanism
- ETH hit 5-loss cooldown → prevented ~6 additional losses
- This IS working better than entry logic

**Keep:** Cooldown is a strong safety feature

---

## 4. Recommendations

### 4.1 Immediate
- [ ] Hard ban entries > 0.95 (both YES and NO)
- [ ] Disable fallback logic

### 4.2 Medium-term  
- [ ] Fix fee calculation (exact formula)
- [ ] Add liquidity depth check (ask_size >= contracts)
- [ ] Add rolling win-rate per coin → size down on losing streaks
- [ ] Flag cheap entries (< $0.01) for manual review
- [ ] FIX: Settings UI has step="5" on min_stake/max_stake inputs - only accepts multiples of 5. Change to step="1" to allow any dollar range.

### 4.3 Research
- [ ] Validate "probability collapse" pattern historically
- [ ] Separate YES vs NO performance per coin
- [ ] Recalibrate confidence scoring

---

## 5. What Works Well

- Probability collapse detection (real edge signal)
- Cooldown/pass mechanism
- Multi-coin diversity
- Paper trading infrastructure

---

## 6. What Needs Work

- Entry band filtering
- Fee calculation accuracy
- Liquidity realism
- Confidence calibration

---

## 7. Live Readiness

**NOT READY** until:
1. Liquidity filter in place
2. Cheap-entry warnings work
3. Fee formula fixed
4. Ban expensive entries

---

## 8. Notes for Later

- Run comparison: current model vs exact fee formula on historical trades
- Backtest cheap-entry edge across longer period
- Test rolling win-rate sizing logic
- Compare to ChatGPT and DeepSeek reports for alignment

---

Files referenced:
- /home/sean/autobet/chatgptreport.md
- /home/sean/autobet/deepseekreport.md