### Changelog
- **v7.7 (Nov 18, 2026):**
  - Added **Regime-Based Sizing (`TREND_BREAKOUT` vs. `RANGE_BOX`)**: deploys `10` lots AM / `6` lots PM (`7/2/1` or `4/1/1`) outside `VAH/VAL` or on `>= 6.0 pt` directional days, and drops to `4` lots (`100%` all-out at `+2.8 pts`) inside narrow ranges—cutting annual round-turn fee drag from `฿208k` to `฿124k` in backtests.
  - Added **Morning One-and-Done Gate (`>= +฿3,000 THB` gross)** before `11:00 BKK` and `>= +฿5,000 THB` full-day lockout.
  - Added **Settrade Streaming `Av.Vol` Unblocked Stop Rule** so resting T2 limit orders never block single-block conditional `MP-MKT` stops.



# Code2Cash (C2C) Execution Framework

Systematic semi-automated trading engines running via GitHub Actions, monitored via Telegram, and executed via InnovestX / TradingView.

## System Matrix

| Engine | Target Asset | Frequency | Schedule (BKK) | Ruleset |
| :--- | :--- | :--- | :--- | :--- |
| **Engine T** | TFEX SET50 Index Futures | 2x / day | 11:45 AM & 14:15 PM | Intermarket (ES), FX slope, 5-cap breadth, DELTA decoupler |
| **Engine E** | US DR/DRx (Vault B) | 1x / day | 20:00 PM | QQQ 4-Regime, 21 EMA bounce, RS > 0, 72h earnings blackout |

## Risk Architecture
* **1R Base Risk:** ฿30,000 THB (Vault B: ฿3.0M line)
* **Streak Boost:** 1.5R (฿45,000 THB) after 3 consecutive wins
* **Max Single Position Outlay:** ฿600,000 THB (20% Vault B cap)
* **Daily Circuit Breaker:** 2 consecutive stop-outs (-2.0R) -> Lockout until next day
* **Weekly Circuit Breaker:** 3 stop-outs in rolling week -> Lockout until Monday
# c2c
