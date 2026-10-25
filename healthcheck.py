import os
import sys
import requests
from datetime import datetime
import pytz
import yfinance as yf

token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("TELEGRAM_CHAT_ID")
bkk_now = datetime.now(pytz.timezone("Asia/Bangkok"))
bkk_time_str = bkk_now.strftime("%Y-%m-%d %H:%M:%S")

# Critical Tickers to verify
audit_tickers = ["^SET50", "DELTA.BK", "THB=X", "QQQ", "NVDA", "ES=F"]
failed = []
passed = []

for ticker in audit_tickers:
    try:
        df = yf.download(ticker, period="3d", interval="1d", progress=False)
        if df is not None and not df.empty and len(df) >= 1:
            passed.append(ticker)
        else:
            failed.append(ticker)
    except Exception as e:
        failed.append(f"{ticker} ({str(e)})")

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
    print("Health check: DEGRADED feeds detected.")
else:
    print(f"Health check: ALL {len(passed)} feeds online. Normal silent pass.")
