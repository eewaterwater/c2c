# Code2Cash (C2C) — Systematic Multi-Engine Trading Architecture

Automated signal detection, regime classification, and Telegram execution ticketing for **TFEX SET50 Index Futures (`Engine T`)**.

## Repository Structure
- `engine_t.py` — Code2Cash **v7.7** TFEX `S50Z26` scanner, regime classifier (`TREND_BREAKOUT` vs. `RANGE_BOX`), risk/sizing calculator, and Telegram alert dispatcher.
- `STRATEGY_V7_7.md` — Complete institutional ruleset, Settrade Streaming `Av.Vol` stop-routing invariant, and session flatten gates.
- `.github/workflows/engine_t.yml` — Automated 15-minute market-hours scheduler (`10:15–15:30 BKK`, Mon–Fri).

## Changelog
- **v7.7 (Nov 18, 2026):**
  - Added **Regime-Based Sizing (`TREND_BREAKOUT` vs. `RANGE_BOX`)**: deploys `10` lots AM / `6` lots PM (`7/2/1` or `4/1/1`) outside `VAH/VAL` or on `>= 6.0 pt` directional days, and drops to `4` lots (`100%` all-out at `+2.8 pts`) inside narrow ranges—cutting annual round-turn fee drag from `฿208k` to `฿124k` in backtests.
  - Added **Morning One-and-Done Gate (`>= +฿3,000 THB` gross)** before `11:00 BKK` and `>= +฿5,000 THB` full-day lockout.
  - Added **Settrade Streaming `Av.Vol` Unblocked Stop Rule** so resting T2 limit orders never block single-block conditional `MP-MKT` stops.
- **v7.6:** Added 15M EMA(9/20) + MA(200) + RSI(14) Telegram alerting and GitHub Actions automation.
