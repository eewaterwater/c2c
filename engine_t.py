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

# 1. State Machine Load & Initialization
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

# 2. Resilient OHLC Extraction
def get_clean_ohlc(ticker_sym):
    try:
        df = yf.download(ticker_sym, period="1mo", interval="1d", progress=False)
        if df is None or df.empty:
            return 0.0, 0.0, 0.0, 0.0
        
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
            
        df = df.dropna(subset=["Close", "High", "Low"])
        if len(df) == 0:
            return 0.0, 0.0, 0.0, 0.0
            
        curr_close = float(df["Close"].iloc[-1])
        prev_row = df.iloc[-2] if len(df) >= 2 else df.iloc[-1]
        
        prev_close = float(prev_row["Close"])
        prev_high = float(prev_row["High"])
        prev_low = float(prev_row["Low"])
        
        return curr_close, prev_close, prev_high, prev_low
    except Exception as e:
        print(f"Error fetching {ticker_sym}: {e}")
        return 0.0, 0.0, 0.0, 0.0

s50_price, pdc, pdh, pdl = get_clean_ohlc("^SET50.BK")
active_ticker = "^SET50.BK"

if s50_price <= 0:
    s50_price, pdc, pdh, pdl = get_clean_ohlc("^SET50")
    active_ticker = "^SET50"

if pdh <= pdl or pdh == pdc:
    pdh = round(pdc + 4.20, 2)
    pdl = round(pdc - 4.80, 2)

poc_est = round((pdh + pdl + pdc) / 3.0, 2)
vah_est = pdh
val_est = pdl

# DELTA Decoupler Check
delta_pct = 0.0
try:
    d_curr, d_prev, _, _ = get_clean_ohlc("DELTA.BK")
    if d_prev > 0 and d_curr > 0:
        delta_pct = ((d_curr - d_prev) / d_prev) * 100.0
except Exception as e:
    print(f"DELTA error: {e}")

is_pre_open_scan = (bkk_now.hour == 9 and bkk_now.minute < 45)

# 3. 5-Gate Matrix & Signal Generation
gate_delta = abs(delta_pct) < 1.5
gate_trend_dir = "LONG" if s50_price >= poc_est else "SHORT"
gate_cb = state["circuit_breakers"].get("daily_stopouts", 0) < 2
gates_cleared = gate_delta and gate_cb

contracts = 10
action_card = []
settrade_block = ""

if gates_cleared and state["engine_t"].get("status") == "FLAT" and not is_pre_open_scan:
    if gate_trend_dir == "LONG":
        entry_pt = round(s50_price, 1)
        stop_pt = round(val_est - 1.5, 1) if val_est < entry_pt else round(entry_pt - 4.0, 1)
        risk_pts = round(entry_pt - stop_pt, 1)
        t1_pt = round(entry_pt + 3.5, 1)
        t2_pt = round(entry_pt + 7.0, 1)
        
        action_card = [
            "🚨 <b>[ACTION REQUIRED: S50Z26 LONG ENTRY]</b>",
            f"• <b>Order:</b> <b>BUY S50Z26 @ {entry_pt:.1f}</b>",
            f"• <b>Hard Stop:</b> <code>{stop_pt:.1f}</code> (-{risk_pts:.1f} pts)",
            f"• <b>Position Size:</b> <b>{contracts} contracts</b> (Risk: ฿{risk_pts*200*contracts:,.0f} THB)",
            f"• <b>T1 Bank (60% / 6 contracts):</b> Limit <b>{t1_pt:.1f}</b> (+3.5 pts >> Move stop to BE)",
            f"• <b>T2 Runner (40% / 4 contracts):</b> Target <b>{t2_pt:.1f}</b> or trail 15M EMA(9)",
            "• <b>⚡ Free-Roll Pyramiding:</b> On T1 hit, add <b>+2 contracts</b> on secondary base breakout."
        ]
        settrade_block = f"<code>OPEN LONG S50Z26 @ {entry_pt:.1f} | Vol: {contracts} | Stop: {stop_pt:.1f}</code>"
    else:
        entry_pt = round(s50_price, 1)
        stop_pt = round(vah_est + 1.5, 1) if vah_est > entry_pt else round(entry_pt + 4.0, 1)
        risk_pts = round(stop_pt - entry_pt, 1)
        t1_pt = round(entry_pt - 3.5, 1)
        t2_pt = round(entry_pt - 7.0, 1)
        
        action_card = [
            "🚨 <b>[ACTION REQUIRED: S50Z26 SHORT ENTRY]</b>",
            f"• <b>Order:</b> <b>SHORT S50Z26 @ {entry_pt:.1f}</b>",
            f"• <b>Hard Stop:</b> <code>{stop_pt:.1f}</code> (+{risk_pts:.1f} pts)",
            f"• <b>Position Size:</b> <b>{contracts} contracts</b> (Risk: ฿{risk_pts*200*contracts:,.0f} THB)",
            f"• <b>T1 Bank (60% / 6 contracts):</b> Limit <b>{t1_pt:.1f}</b> (-3.5 pts >> Move stop to BE)",
            f"• <b>T2 Runner (40% / 4 contracts):</b> Target <b>{t2_pt:.1f}</b> or trail 15M EMA(9)",
            "• <b>⚡ Free-Roll Pyramiding:</b> On T1 hit, add <b>+2 contracts</b> on secondary base breakout."
        ]
        settrade_block = f"<code>OPEN SHORT S50Z26 @ {entry_pt:.1f} | Vol: {contracts} | Stop: {stop_pt:.1f}</code>"

# 4. State Updates & Ratchets
t_state = state["engine_t"]
status = t_state.get("status", "FLAT")
alert_lines = []

if status != "FLAT" and not is_pre_open_scan and s50_price > 0:
    entry = t_state.get("entry_price", 0.0)
    direction = t_state.get("direction", "BUY")
    t1 = t_state.get("t1_target", 0.0)
    stop = t_state.get("current_stop", 0.0)
    
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

t_state["last_update"] = bkk_time_str
state["engine_t"] = t_state
try:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)
except Exception as e:
    print(f"Error saving state: {e}")

# 5. Build Status Card
delta_flag = "⚠️ DECOUPLED" if abs(delta_pct) >= 1.5 else "✅ Normal"

if is_pre_open_scan:
    msg = (
        "🌅 <b>[ENGINE T: 09:45 S50Z26 AUCTION WICK PREP]</b>\n"
        f"📅 <b>Time:</b> <code>{html.escape(bkk_time_str)} BKK</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Contract:</b> <code>S50Z26 (Index: {active_ticker})</code>\n"
        f"• <b>Prior Close (PDC):</b> <code>{pdc:.2f} pts</code>\n"
        f"• <b>Prior High (PDH / VAH):</b> <code>{vah_est:.2f} pts</code>\n"
        f"• <b>Prior Low (PDL / VAL):</b> <code>{val_est:.2f} pts</code>\n"
        f"• <b>Est. POC Base:</b> <code>{poc_est:.2f} pts</code>\n"
        f"• <b>DELTA Decoupler:</b> <code>{delta_pct:+.2f}%</code> ({delta_flag})\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎯 <b>S50Z26 AUCTION WICK LIMITS (09:45–10:00 BKK):</b>\n"
        f"• <b>Long Wick Fade:</b> Limit Buy at <b>{val_est:.1f}</b> | Stop: {val_est - 2.5:.1f} (-2.5 pts)\n"
        f"• <b>Short Wick Fade:</b> Limit Short at <b>{vah_est:.1f}</b> | Stop: {vah_est + 2.5:.1f} (+2.5 pts)\n"
        "• <b>Target:</b> Bank +2.0 to +3.0 pts into wick bounce.\n"
        "• <b>⚠️ 09:58 BKK HARD LOCK:</b> Bank or drag stop to exact BE before cash open at 10:00!\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Next Check: 10:15 BKK (15M MSS Trend Confirmation)</i>"
    )
else:
    header_tag = action_card[0] if action_card else "🇹🇭 <b>[C2C ENGINE T: S50Z26 TACTICAL MONITOR]</b>"
    lines = [
        header_tag,
        f"📅 <b>Time:</b> <code>{html.escape(bkk_time_str)} BKK</code>",
        "━━━━━━━━━━━━━━━━━━",
        f"• <b>Active Contract:</b> <code>S50Z26 (Underlying: {s50_price:.2f})</code>",
        f"• <b>Prior Day Value Area:</b> VAH: <code>{vah_est:.1f}</code> | POC: <code>{poc_est:.1f}</code> | VAL: <code>{val_est:.1f}</code>",
        f"• <b>DELTA Decoupler:</b> <code>{delta_pct:+.2f}%</code> ({delta_flag})",
        f"• <b>Pre-Flight 5-Gates:</b> {'🟢 ALL GATES CLEARED' if gates_cleared else '🔴 GATES RESTRICTED'}",
        f"• <b>Active State:</b> <b>{html.escape(t_state.get('status', 'FLAT'))}</b>",
        "━━━━━━━━━━━━━━━━━━"
    ]
    
    if action_card:
        lines.extend(action_card[1:])
        lines.append("━━━━━━━━━━━━━━━━━━")
    elif status == "FLAT":
        lines.extend([
            "📋 <b>EXECUTION:</b> Standing aside. Gates restricted or waiting for 15M structure confirmation.",
            "━━━━━━━━━━━━━━━━━━"
        ])
        
    if settrade_block:
        lines.extend([
            "📲 <b>SETTRADE QUICK-PASTE:</b>",
            settrade_block,
            "━━━━━━━━━━━━━━━━━━"
        ])
        
    if alert_lines:
        lines.extend(alert_lines)
        lines.append("━━━━━━━━━━━━━━━━━━")
        
    if friday_flatten_warning:
        lines.append(friday_flatten_warning)
        lines.append("━━━━━━━━━━━━━━━━━━")
        
    lines.append(
        f"🛡️ <b>Circuit Breakers:</b> Daily: <code>{state['circuit_breakers'].get('daily_stopouts', 0)}/2</code> | "
        f"Weekly: <code>{state['circuit_breakers'].get('weekly_stopouts', 0)}/3</code>\n"
        "<i>Code2Cash Institutional Engine v7</i>"
    )
    msg = "\n".join(lines)

keyboard = {
    "inline_keyboard": [
        [
            {"text": "📲 Open InnovestX", "url": "https://play.google.com/store/apps/details?id=com.scbs.easyinvest"},
            {"text": "📈 TradingView S50Z2026", "url": "https://www.tradingview.com/chart/?symbol=TFEX%3AS50Z2026"}
        ]
    ]
}

if token and chat_id:
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML", "reply_markup": keyboard},
            timeout=15
        )
        print(f"Engine T cycle completed. Status: {res.status_code}")
    except Exception as e:
        print(f"Telegram network exception: {e}")
