# C2C Engine (S50 swing alerts)

Telegram **alert-only** engine for TFEX SET50 futures. It never places orders.
Runs on GitHub Actions at 11:45 and 14:15 Bangkok time (Mon-Fri), checks the regime,
breadth, intermarket and event gates, and either sends a trade plan or a "no trade" status.

## Setup
1. Repo -> Settings -> Secrets and variables -> Actions -> add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
2. Fill the macro calendar in `config/settings.py` (BOT MPC, US CPI, FOMC dates). It ships empty.
3. Actions tab -> "C2C S50 Scheduled Runner" -> Run workflow.

## Local
```
pip install -r requirements.txt
python -m pytest -q
python engine_worker.py --dry-run      # prints instead of sending, saves nothing
```

## Known limits
- Data is Yahoo Finance. S50 futures are not on Yahoo, so the SET50 index is the price proxy.
  Verify `INDEX_TICKER` returns data before trusting any signal.
- GitHub cron can start several minutes late or skip runs.
- Position state lives in `state/state.json` (tracked from alerts, not from your broker fills).
- Rules are unbacktested. Treat signals as hypotheses.
