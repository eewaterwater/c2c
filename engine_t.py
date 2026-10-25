import os
import sys
import json
import html
import requests
from datetime import datetime
import pytz
import pandas as pd
import numpy as np
import yfinance as yf

token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("TELEGRAM_CHAT_ID")
bkk_now = datetime.now(pytz.timezone("Asia/Bangkok"))
bkk_time_str = bkk_now.strftime("%Y-%m-%d %H:%M:%S")
is_friday = (bkk_now.weekday() == 4)

# 1. State Machine Load & Validation
state_file = "state.json"
default_state = {
    "engine_t": {
        "status": "FLAT",               # FLAT, ACTIVE_INITIAL, T1_BANKED_BE, PYRAMIDED
        "direction": None,             # BUY or SELL
        "entry_price": 0.0,
        "current_stop": 0.0,
        "initial_stop": 0.0,
        "t1_target": 0.0,
        "contracts_open": 0,
        "t1_banked_pts": 0.0,
        "pyramid_eligible": False,
        "last_update": bkk_time_str
    },
    "circuit_breakers": {
        "daily_stopouts": 0,
        "weekly_stopouts": 0,
        "date": bkk_now.strftime("%Y-%m-%d")
    }
}

state = default_state
if os.path.exists(state_file):
    try:
        with open(state_file, "r") as f:
            state = json.load(f)
            if "engine_t" not in state:
                state["engine_t"] = default_state["engine_t"]
    except Exception as e:
        print(f"State load note: {e}")

# Circuit breaker daily reset
if state.get("circuit_breakers", {}).get("date") != bkk_now.strftime("%Y-%m-%d"):
    state["circuit_breakers"]["daily_stopouts"] = 0
    state["circuit_breakers"]["date"] = bkk_now.strftime("%Y-%m-%d")

# 2. Market Feeds: SET50, DELTA Decoupler & Top 5 Breadth
delta_pct = 0.0
s50_price = 0.0

try:
    s50_df = yf.download("^SET50", period="1mo", interval="1d", progress=False)
    if s50_df is not None and not s50_df.empty:
        c = s50_df["Close"].iloc[:, 0].dropna() if isinstance(s50_df.columns, pd.MultiIndex) else s50_df["Close"].dropna()
        if len(c) > 0:
            s50_price = float(c.iloc[-1])
except Exception:
    pass

try:
    delta_df = yf.download("DELTA.BK", period="1mo", interval="1d", progress=False)
    if delta_df is not None and not delta_df.empty:
        c_del = delta_df["Close"].iloc[:, 0].dropna() if isinstance(delta_df.columns, pd.MultiIndex) else delta_df["Close"].dropna()
        if len(c_del) >= 2:
            delta_pct = ((float(c_del.iloc[-1]) - float(c_del.iloc[-2])) / float(c_del.iloc[-2])) * 100.0
except Exception:
    pass

# 3. Process State Machine & Breakeven Ratchets
t_state = state["engine_t"]
status = t_state.get("status", "FLAT")
alert_lines = []

if status != "FLAT" and s50_price > 0:
    entry = t_state["entry_price"]
    direction = t_state["direction"]
    t1 = t_state["t1_target"]
    stop = t_state["current_stop"]
    
    # Check T1 Bank Target
    if status == "ACTIVE_INITIAL":
        t1_hit = (s50_price >= t1) if direction == "BUY" else (s50_price <= t1)
        if t1_hit:
            t_state["status"] = "T1_BANKED_BE"
            t_state["current_stop"] = entry  # Move stop to exact breakeven
            t_state["contracts_open"] -= 4   # Bank 4 contracts
            pts_banked = abs(t1 - entry)
            t_state["t1_banked_pts"] = pts_banked
            t_state["pyramid_eligible"] = True
            
            alert_lines.append(f"🎯 <b>[TRANCHE 1 BANKED: +{pts_banked:.1f} PTS]</b>")
            alert_lines.append("• <b>Remaining 6 Contracts:</b> Stop pulled to Breakeven (0.0 pt).")
            alert_lines.append("• <b>Pyramiding Status:</b> 🟢 AUTHORIZED (+2 contracts on secondary base).")

    # Check Stop Loss
    stop_hit = (s50_price <= stop) if direction == "BUY" else (s50_price >= stop)
    if stop_hit:
        loss_pts = abs(entry - stop) if status == "ACTIVE_INITIAL" else 0.0
        t_state["status"] = "FLAT"
        t_state["contracts_open"] = 0
        t_state["pyramid_eligible"] = False
        
        if status == "ACTIVE_INITIAL":
            state["circuit_breakers"]["daily_stopouts"] += 1
            state["circuit_breakers"]["weekly_stopouts"] += 1
            alert_lines.append(f"🛑 <b>[STOP-OUT HIT: -{loss_pts:.1f} PTS]</b>")
        else:
            alert_lines.append("🛡️ <b>[BREAKEVEN EXIT]</b> Tranche 2 stopped at 0.0 pt. Zero principal loss.")

# Friday 16:15 Flatten Directive Check
friday_flatten_warning = ""
if is_friday and bkk_now.hour >= 16 and status == "ACTIVE_INITIAL":
    friday_flatten_warning = (
        "⚠️ <b>[FRIDAY 16:15 FLATTEN HOOK]</b>\n"
        "• Tranche 1 has not banked before session close.\n"
        "• Playbook Rule: <b>Flatten all open contracts before 16:30 BKK</b> to eliminate unhedged weekend gap risk."
    )

# 4. Save Persistent State Machine
t_state["last_update"] = bkk_time_str
state["engine_t"] = t_state
try:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)
except Exception as e:
    print(f"Error saving state: {e}")

# 5. Build Telegram Status Card
delta_flag = "⚠️ DECOUPLED" if abs(delta_pct) >= 1.5 else "✅ Normal"
msg = (
    "🇹🇭 <b>[C2C ENGINE T: S50 TACTICAL MONITOR]</b>\n"
    f"📅 <b>Time:</b> <code>{html.escape(bkk_time_str)} BKK</code>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    f"• <b>SET50 Level:</b> <code>{s50_price:.2f} pts</code>\n"
    f"• <b>DELTA Movement:</b> <code>{delta_pct:+.2f}%</code> ({delta_flag})\n"
    f"• <b>Active State:</b> <b>{html.escape(t_state['status'])}</b>\n"
    f"• <b>Open Contracts:</b> <code>{t_state['contracts_open']}</code> | "
    f"<b>Direction:</b> <code>{html.escape(str(t_state['direction']))}</code>\n"
    f"• <b>Active Stop:</b> <code>{t_state['current_stop']:.1f}</code> | "
    f"<b>Pyramid Eligible:</b> {'🟢 YES' if t_state['pyramid_eligible'] else '⚪ NO'}\n"
    "━━━━━━━━━━━━━━━━━━\n"
)

if alert_lines:
    msg += "\n".join(alert_lines) + "\n━━━━━━━━━━━━━━━━━━\n"

if friday_flatten_warning:
    msg += friday_flatten_warning + "\n━━━━━━━━━━━━━━━━━━\n"

msg += (
    f"🛡️ <b>Circuit Breakers:</b> Daily: <code>{state['circuit_breakers']['daily_stopouts']}/2</code> | "
    f"Weekly: <code>{state['circuit_breakers']['weekly_stopouts']}/3</code>\n"
    "<i>Code2Cash Institutional Engine v7</i>"
)

keyboard = {
    "inline_keyboard": [
        [
            {"text": "📲 Open InnovestX", "url": "https://play.google.com/store/apps/details?id=com.scbs.easyinvest"},
            {"text": "📈 TradingView S50", "url": "https://www.tradingview.com/chart/?symbol=TFEX%3AS501!"}
        ]
    ]
}

if token and chat_id:
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML", "reply_markup": keyboard},
        timeout=15
    )
    print("Engine T execution cycle completed.")
