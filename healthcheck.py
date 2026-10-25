import os
import requests
from datetime import datetime
import pytz
import yfinance as yf

token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("TELEGRAM_CHAT_ID")
bkk_now = datetime.now(pytz.timezone("Asia/Bangkok"))
bkk_time_str = bkk_now.strftime("%Y-%m-%d %H:%M:%S")

# Tickers to audit
audit_tickers = [
    {"name": "SET50 Index", "symbols": ["^SET50", "^SET.BK"]},
    {"name": "DELTA.BK", "symbols": ["DELTA.BK"]},
    {"name": "USD/THB", "symbols": ["THB=X"]},
    {"name": "QQQ", "symbols": ["QQQ"]},
    {"name": "NVDA", "symbols": ["NVDA"]},
    {"name": "ES Futures", "symbols": ["ES=F"]}
]

failed = []
passed = []

for item in audit_tickers:
    name = item["name"]
    is_ok = False
    # Check symbols with a 1-month lookback to safely handle holiday weekends
    for sym in item["symbols"]:
        try:
            df = yf.download(sym, period="1mo", interval="1d", progress=False)
            if df is not None and not df.empty and len(df) >= 1:
                passed.append(name)
                is_ok = True
                break
        except Exception:
            continue
    if not is_ok:
        failed.append(name)

is_degraded = len(failed) > 0

if is_degraded:
    msg = (
        "⚠️ <b>[PRE-FLIGHT HEALTH ALERT: DATA DEGRADED]</b>\n"
        f"📅 <b>Time:</b> <code>{bkk_time_str} BKK</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Status:</b> Warning! {len(failed)} feed(s) unverified.\n"
        f"• <b>Failed Tickers:</b> {', '.join(failed)}\n"
        f"• <b>Healthy Feeds:</b> {', '.join(passed)}\n"
        "• <b>Action:</b> Verify data feeds prior to market open.\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Pre-flight check via GitHub Cloud.</i>"
    )
    if token and chat_id:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=15
        )
    print(f"Health check: DEGRADED feeds detected ({failed}).")
else:
    print(f"Health check: ALL {len(passed)} feeds online. Normal silent pass.")
