import os
import json
import requests
from datetime import datetime
import pytz

token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("TELEGRAM_CHAT_ID")
bkk_now = datetime.now(pytz.timezone("Asia/Bangkok"))
bkk_time_str = bkk_now.strftime("%Y-%m-%d %H:%M:%S")

# 1. Read & Reset State
state_path = "state.json"
state = {
    "consecutive_wins": 0,
    "daily_stopouts": 0,
    "weekly_stopouts": 0,
    "last_trade_date": bkk_now.strftime("%Y-%m-%d"),
    "active_position": None
}

if os.path.exists(state_path):
    try:
        with open(state_path, "r") as f:
            state = json.load(f)
    except Exception as e:
        print(f"Error reading state: {e}")

prior_streak = state.get("consecutive_wins", 0)
prior_weekly_stops = state.get("weekly_stopouts", 0)

# Weekly Reset Protocol (Master Sheet 05_TREASURY_SWEEPER)
state["daily_stopouts"] = 0
state["weekly_stopouts"] = 0

try:
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)
except Exception as e:
    print(f"Error saving state: {e}")

# 2. Build Treasury Sweeper Message
recap_lines = [
    "🏛️ <b>[CODE2CASH TREASURY SWEEPER & WEEKEND RECAP]</b>",
    f"📅 <b>Time:</b> {bkk_time_str} BKK (Friday SET Close)",
    "━━━━━━━━━━━━━━━━━━",
    "📊 <b>WEEKLY RISK ENGINE AUDIT:</b>",
    f"• Ending Win Streak: <b>{prior_streak}</b>",
    f"• Recorded Weekly Stop-Outs: <b>{prior_weekly_stops}</b>",
    "• Circuit Breaker Status: <b>CLEARED & READY FOR MONDAY</b>",
    "━━━━━━━━━━━━━━━━━━",
    "💰 <b>CAPITAL SWEEP DIRECTIVE (Master Playbook):</b>",
    "• <b>Engine T Baseline:</b> Retain ฿1.0M working margin in TFEX.",
    "• <b>Vault B Baseline:</b> Retain ฿3.0M working line in DRx/USD.",
    "• <b>Protocol:</b> Transfer 100% of net realized profits above baseline into defensive cash / <b>SCBMONEY</b>.",
    "━━━━━━━━━━━━━━━━━━",
    "🏖️ <i>All TFEX intraday positions closed. Standing down for the weekend.</i>"
]

full_msg = "\n".join(recap_lines)

keyboard = {
    "inline_keyboard": [
        [
            {"text": "📲 Open InnovestX", "url": "https://play.google.com/store/apps/details?id=com.scbs.easyinvest"},
            {"text": "📈 TradingView", "url": "https://www.tradingview.com"}
        ]
    ]
}

if token and chat_id:
    res = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": full_msg,
            "parse_mode": "HTML",
            "reply_markup": keyboard
        },
        timeout=15
    )
    print(f"Telegram status: {res.status_code}")
  
