import os
import json
import requests
from datetime import datetime
import pytz
import yfinance as yf
import pandas as pd

token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("TELEGRAM_CHAT_ID")
bkk_now = datetime.now(pytz.timezone("Asia/Bangkok"))
bkk_time_str = bkk_now.strftime("%Y-%m-%d %H:%M:%S")

# Read Engine E State
win_streak = 0
daily_stops = 0
weekly_stops = 0
if os.path.exists("state.json"):
    try:
        with open("state.json", "r") as f:
            st = json.load(f)
            win_streak = st.get("consecutive_wins", 0)
            daily_stops = st.get("daily_stopouts", 0)
            weekly_stops = st.get("weekly_stopouts", 0)
    except Exception:
        pass

# Quick Close Checks
s50_close = "N/A"
qqq_close = "N/A"
try:
    df_s50 = yf.download("^SET50", period="3d", interval="1d", progress=False)
    if df_s50 is not None and not df_s50.empty:
        c = df_s50["Close"].iloc[:, 0].dropna() if isinstance(df_s50.columns, pd.MultiIndex) else df_s50["Close"].dropna()
        s50_close = f"{float(c.iloc[-1]):.2f} pts"
except Exception:
    pass

try:
    df_qqq = yf.download("QQQ", period="3d", interval="1d", progress=False)
    if df_qqq is not None and not df_qqq.empty:
        c = df_qqq["Close"].iloc[:, 0].dropna() if isinstance(df_qqq.columns, pd.MultiIndex) else df_qqq["Close"].dropna()
        qqq_close = f"${float(c.iloc[-1]):.2f}"
except Exception:
    pass

msg = (
    "🌙 <b>[C2C CONSOLIDATED EVENING BRIEFING]</b>\n"
    f"📅 <b>Date:</b> <code>{bkk_time_str} BKK</code>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "📊 <b>END-OF-DAY CLOSES:</b>\n"
    f"• <b>SET50 Index:</b> {s50_close}\n"
    f"• <b>US Tech (QQQ):</b> {qqq_close}\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🛡️ <b>CIRCUIT BREAKER & RISK STATUS:</b>\n"
    f"• <b>Engine E Win Streak:</b> {win_streak}W\n"
    f"• <b>Daily Stop-Outs:</b> {daily_stops}/2\n"
    f"• <b>Weekly Stop-Outs:</b> {weekly_stops}/3 (Rolling)\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🏦 <b>TREASURY & MARGIN AUDIT:</b>\n"
    "• <b>Working TFEX Line:</b> ฿300,000 THB (InnovestX)\n"
    "• <b>Core Treasury Vault:</b> ฿700,000 THB in SCB EZ (1.45% p.a.)\n"
    "• <b>Accrued Daily MMF Yield:</b> ~+฿28.0 THB/day (+฿840/mo)\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "<i>All systematic pipelines resting until 09:15 BKK pre-flight.</i>"
)

keyboard = {
    "inline_keyboard": [
        [
            {"text": "📲 Open InnovestX", "url": "https://play.google.com/store/apps/details?id=com.scbs.easyinvest"},
            {"text": "🏦 Open SCB EASY", "url": "https://play.google.com/store/apps/details?id=com.scb.phone"}
        ]
    ]
}

if token and chat_id:
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML", "reply_markup": keyboard},
        timeout=15
    )
    print("Daily summary delivered.")
