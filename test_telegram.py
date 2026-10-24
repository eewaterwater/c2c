"""Run once to confirm your bot token and chat id work: python test_telegram.py"""
from core.notifier import send

ok = send("C2C engine connectivity test: if you can read this, Telegram is wired up.")
print("Sent." if ok else "Not sent: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")
