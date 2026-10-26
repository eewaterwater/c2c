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
today_str = bkk_now.strftime("%Y-%m-%d")
is_friday = (bkk_now.weekday() == 4)

# 1. State Machine Load & Key Initialization
state_file = "state.json"
state = {}

if os.path.exists(state_file):
    try:
        with open(state_file, "r") as f:
            state = json.load(f)
    except Exception as e:
        print(f"State load warning: {e}")

if "circuit_breakers" not in state or not isinstance(state["circuit_breakers"], dict):
    state["circuit_breakers"] = {
        "daily_stopouts": state.get("daily_stopouts", 0),
        "weekly_stopouts": state.get("weekly_stopouts", 0),
        "date": today_str
    }

if "engine_t" not in state or not isinstance(state["engine_t"], dict):
    state["engine_t"] = {
        "status": "FLAT",
        "direction": None,
        "entry_price": 0.0,
        "current_stop": 0.0,
        "initial_stop": 0.0,
        "t1_target": 0.0,
        "contracts_open": 0,
        "t1_banked_pts": 0.0,
        "pyramid_eligible": False,
        "last_update": bkk_time_str
    }

if state["circuit_breakers"].get("date") != today_str:
    state["circuit_breakers"]["daily_stopouts"] = 0
    state["circuit_breakers"]["date"] = today_str

# 2. Market Feeds: S50Z26 (Futures), SET50 (Cash), and DELTA
s50_symbol = "S50Z26.BK"
s50_price = 0.0
pdh = 0.0
pdl = 0.0
pdc = 0.0
is_direct_contract = False

def extract_ohlc(df):
    if df is None or df.empty:
        return 0.0, 0.0, 0.0, 0.0
    try:
        c = df["Close"].iloc[:, 0].dropna() if isinstance(df.columns, pd.MultiIndex) else df["Close"].dropna()
        h = df["High"].iloc[:, 0].dropna() if isinstance(df.columns, pd.MultiIndex) else df["High"].dropna()
        l = df["Low"].iloc[:, 0].dropna() if isinstance(df.columns, pd.MultiIndex) else df["Low"].dropna()
        curr = float(c.iloc[-1]) if len(c) > 0 else 0.0
        prev_c = float(c.iloc[-2]) if len(c) >= 2 else curr
        prev_h = float(h.iloc[-2]) if len(h) >= 2 else curr
        prev_l = float(l.iloc[-2]) if len(l) >= 2 else curr
        return curr, prev_c, prev_h, prev_l
    except Exception:
        return 0.0, 0.0, 0.0, 0.0

# Try specific contract ticker variations on Yahoo Finance
for ticker_variant in ["S50Z26.BK", "S50Z26.TX", "S50Z2026.BK"]:
    try:
        fut_df = yf.download(ticker_variant, period="1mo", interval="1d", progress=False)
        p, c, h, l = extract_ohlc(fut_df)
        if p > 0:
            s50_price, pdc, pdh, pdl = p, c, h, l
            s50_symbol = ticker_variant
            is_direct_contract = True
            break
    except Exception:
        pass

# Fallback to ^SET50 cash index if the individual futures contract feed is not indexed
set50_cash = 0.0
try:
    cash_df = yf.download("^SET50", period="1mo", interval="1d", progress=False)
    p_cash, c_cash, h_cash, l_cash = extract_ohlc(cash_df)
    set50_cash = p_cash
    if not is_direct_contract and p_cash > 0:
        s50_price, pdc, pdh, pdl = p_cash, c_cash, h_cash, l_cash
        s50_symbol = "S50Z26 (Mapped from ^SET50)"
except Exception:
    pass

# Basis Calculation (Futures Price - Cash Index)
basis_str = "N/A"
if is_direct_contract and set50_cash > 0:
    basis = s50_price - set50_cash
    basis_str = f"{basis:+.2f} pts"

delta_pct = 0.0
try:
    delta_df = yf.download("DELTA.BK", period="1mo", interval="1d", progress=False)
    if delta_df is not None and not delta_df.empty:
        c_del = delta_df["Close"].iloc[:, 0].dropna() if isinstance(delta_df.columns, pd.MultiIndex) else delta_df["Close"].dropna()
        if len(c_del) >= 2:
            delta_pct = ((float(c_del.iloc[-1]) - float(c_del.iloc[-2])) / float(c_del.iloc[-2])) * 100.0
except Exception:
    pass

is_pre_open_scan = (bkk_now.hour == 9 and bkk_now.minute < 45)

# Value Area / Support & Resistance Estimations
poc_est = (pdh + pdl + pdc) / 3.0 if (pdh and pdl and pdc) else s50_price
vah_est = pdh if pdh > 0 else s50_price + 3.0
val_est = pdl if pdl > 0 else s50_price - 3.0

# 3. Process State Machine & Breakeven Ratchets
t_state = state["engine_t"]
status = t_state.get("status", "FLAT")
alert_lines = []

if status != "FLAT" and s50_price > 0 and not is_pre_open_scan:
    entry = t_state.get("entry_price", 0.0)
    direction = t_state.get("direction", "BUY")
    t1 = t_state.get("t1_target", 0.0)
    stop = t_state.get("current_stop", 0.0)
    
    # Check T1 Bank Target
    if status == "ACTIVE_INITIAL":
        t1_hit = (s50_price >= t1) if direction == "BUY" else (s50_price <= t1)
        if t1_hit:
            t_state["status"] = "T1_BANKED_BE"
            t_state["current_stop"] = entry
            t_state["contracts_open"] = max(0, t_state.get("contracts_open", 10) - 4)
            pts_banked = abs(t1 - entry)
            t_state["t1_banked_pts"] = pts_banked
            t_state["pyramid_eligible"] = True
            
            alert_lines.append(f"🎯 <b>[TRANCHE 1 BANKED: +{pts_banked:.1f} PTS]</b>")
            alert_lines.append("• <b>Remaining Contracts:</b> Stop pulled to Breakeven (0.0 pt).")
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

if is_pre_open_scan:
    msg = (
        "🌅 <b>[ENGINE T: 09:45 S50Z26 AUCTION WICK PREP]</b>\n"
        f"📅 <b>Time:</b> <code>{html.escape(bkk_time_str)} BKK</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Contract:</b> <code>{html.escape(s50_symbol)}</code>\n"
        f"• <b>Prior Close (PDC):</b> <code>{pdc:.2f} pts</code>\n"
        f"• <b>Prior High (PDH / VAH):</b> <code>{vah_est:.2f} pts</code>\n"
        f"• <b>Prior Low (PDL / VAL):</b> <code>{val_est:.2f} pts</code>\n"
        f"• <b>Futures Basis (S50 - Cash):</b> <code>{basis_str}</code>\n"
        f"• <b>DELTA Decoupler:</b> <code>{delta_pct:+.2f}%</code> ({delta_flag})\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎯 <b>S50Z26 AUCTION WICK LIMITS (09:45–10:00 BKK):</b>\n"
        f"• <b>Long Wick Fade:</b> Limit Buy at <b>{val_est:.1f}</b> | Stop: {val_est - 2.0:.1f} (-2.0 pts)\n"
        f"• <b>Short Wick Fade:</b> Limit Short at <b>{vah_est:.1f}</b> | Stop: {vah_est + 2.0:.1f} (+2.0 pts)\n"
        "• <b>Target:</b> Bank +2.0 to +3.0 pts into initial wick rebound.\n"
        "• <b>⚠️ 09:58 BKK HARD LOCK:</b> Bank or drag stop to exact BE before cash open at 10:00!\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Next Check: 10:15 BKK (15M MSS Trend Confirmation)</i>"
    )
else:
    msg = (
        "🇹🇭 <b>[C2C ENGINE T: S50Z26 TACTICAL MONITOR]</b>\n"
        f"📅 <b>Time:</b> <code>{html.escape(bkk_time_str)} BKK</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Active Contract:</b> <code>{html.escape(s50_symbol)}</code>\n"
        f"• <b>Current Level:</b> <code>{s50_price:.2f} pts</code>\n"
        f"• <b>Futures Basis:</b> <code>{basis_str}</code>\n"
        f"• <b>DELTA Movement:</b> <code>{delta_pct:+.2f}%</code> ({delta_flag})\n"
        f"• <b>Active State:</b> <b>{html.escape(t_state.get('status', 'FLAT'))}</b>\n"
        f"• <b>Open Contracts:</b> <code>{t_state.get('contracts_open', 0)}</code> | "
        f"<b>Direction:</b> <code>{html.escape(str(t_state.get('direction', 'NONE')))}</code>\n"
        f"• <b>Active Stop:</b> <code>{t_state.get('current_stop', 0.0):.1f}</code> | "
        f"<b>Pyramid Eligible:</b> {'🟢 YES' if t_state.get('pyramid_eligible') else '⚪ NO'}\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )

if alert_lines:
    msg += "\n".join(alert_lines) + "\n━━━━━━━━━━━━━━━━━━\n"

if friday_flatten_warning:
    msg += friday_flatten_warning + "\n━━━━━━━━━━━━━━━━━━\n"

msg += (
    f"🛡️ <b>Circuit Breakers:</b> Daily: <code>{state['circuit_breakers'].get('daily_stopouts', 0)}/2</code> | "
    f"Weekly: <code>{state['circuit_breakers'].get('weekly_stopouts', 0)}/3</code>\n"
    "<i>Code2Cash Institutional Engine v7</i>"
)

keyboard = {
    "inline_keyboard": [
        [
            {"text": "📲 Open InnovestX", "url": "https://play.google.com/store/apps/details?id=com.scbs.easyinvest"},
            {"text": "📈 TradingView S50Z2026", "url": "https://www.tradingview.com/chart/?symbol=TFEX%3AS50Z2026"}
        ]
    ]
}

if token and chat_id:
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML", "reply_markup": keyboard},
        timeout=15
    )
    print("Engine T S50Z26 execution cycle completed.")
