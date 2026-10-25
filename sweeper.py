import os
import requests
from datetime import datetime
import pytz

token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("TELEGRAM_CHAT_ID")
bkk_now = datetime.now(pytz.timezone("Asia/Bangkok"))
bkk_time_str = bkk_now.strftime("%Y-%m-%d %H:%M:%S")

msg = (
    "🧹 <b>[C2C WEEKLY PROFIT SWEEPER PROTOCOL]</b>\n"
    f"📅 <b>Audit Time:</b> <code>{bkk_time_str} BKK</code>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🛡️ <b>MANDATORY FRIDAY CAPITAL AUDIT:</b>\n"
    "• <b>TFEX S50 Session:</b> CLOSED for the weekend.\n"
    "• <b>Baseline Capital Target:</b> ฿1,000,000 THB\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "📋 <b>ACTION PROTOCOL:</b>\n"
    "1. Open <b>InnovestX</b> and check Net TFEX Equity.\n"
    "2. If Total Equity &gt; ฿1,000,000 THB:\n"
    "   ↳ <b>Withdraw 100% of excess profit</b> via InnovestX.\n"
    "   ↳ Deposit directly into <b>SCB EZ Savings</b> (or SCBSFFPLUS).\n"
    "3. If Total Equity &lt; ฿1,000,000 THB:\n"
    "   ↳ Replenish margin buffer from SCB EZ back to ฿300,000 working line.\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🔄 <b>Weekly Stop-Out Counters:</b> RESET for Monday open.\n"
    "<i>Capital insulated. Zero unmanaged weekend risk.</i>"
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
        json={
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "HTML",
            "reply_markup": keyboard
        },
        timeout=15
    )
    print("Sweeper briefing delivered.")
else:
    print("Missing Telegram credentials.")
