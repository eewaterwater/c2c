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

MAX_RISK_PTS = 3.5  # Institutional hard cap (฿7,000 THB max on 10 contracts)

# 1. State Machine Load & Resilient Initialization
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

# 2. Resilient OHLC Data Fetching (Daily & 15-Minute Bars)
def get_clean_df(ticker_sym, period="1mo", interval="1d"):
    try:
        df = yf.download(ticker_sym, period=period, interval=interval, progress=False)
        if df is None or df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna(subset=["Close", "High", "Low"])
    except Exception as e:
        print(f"Fetch error {ticker_sym} ({interval}): {e}")
        return pd.DataFrame()

# Prior Day Values (Daily Basis)
df_daily = get_clean_df("^SET50.BK", period="1mo", interval="1d")
if df_daily.empty:
    df_daily = get_clean_df("^SET50", period="1mo", interval="1d")

s50_price, pdc, pdh, pdl = 0.0, 0.0, 0.0, 0.0
active_ticker = "^SET50.BK"

if not df_daily.empty:
    s50_price = float(df_daily["Close"].iloc[-1])
    prev_row = df_daily.iloc[-2] if len(df_daily) >= 2 else df_daily.iloc[-1]
    pdc = float(prev_row["Close"])
    pdh = float(prev_row["High"])
    pdl = float(prev_row["Low"])

if pdh <= pdl or pdh == pdc:
    pdh = round(pdc + 4.20, 2)
    pdl = round(pdc - 4.80, 2)

poc_est = round((pdh + pdl + pdc) / 3.0, 2)
vah_est = pdh
val_est = pdl

# 15M Intraday Bars & MA200 Calculation
df_15m = get_clean_df("^SET50.BK", period="1mo", interval="15m")
if df_15m.empty:
    df_15m = get_clean_df("^SET50", period="1mo", interval="15m")

ma200_15m = 0.0
closed_close = s50_price

if not df_15m.empty:
    # Ensure calculation on closed bar (iloc[-2] if latest bar is forming, else iloc[-1])
    closed_bar = df_15m.iloc[-2] if len(df_15m) >= 2 else df_15m.iloc[-1]
    closed_close = float(closed_bar["Close"])
    
    # Calculate 200 SMA on 15M bars
    if len(df_15m) >= 200:
        ma200_15m = round(float(df_15m["Close"].rolling(200).mean().iloc[-2]), 2)
    else:
        # Fallback to available length if dataset is slightly short
        ma200_15m = round(float(df_15m["Close"].mean()), 2)

# DELTA Decoupler Check
delta_pct = 0.0
try:
    df_delta = get_clean_df("DELTA.BK", period="5d", interval="1d")
    if len(df_delta) >= 2:
        d_curr = float(df_delta["Close"].iloc[-1])
        d_prev = float(df_delta["Close"].iloc[-2])
        if d_prev > 0:
            delta_pct = ((d_curr - d_prev) / d_prev) * 100.0
except Exception as e:
    print(f"DELTA check error: {e}")

is_pre_open_scan = (bkk_now.hour == 9 and bkk_now.minute < 45)

# 3. 5-Gate Institutional Matrix & Veto Filters
gate_delta = abs(delta_pct) < 1.5
gate_cb = state["circuit_breakers"].get("daily_stopouts", 0) < 2

# Determine Primary Signal from POC/Value Area
prelim_direction = "LONG" if closed_close >= poc_est else "SHORT"

# VETO 1: MA200 Trend Alignment Gate
gate_ma200 = True
ma200_veto_reason = ""
if ma200_15m > 0:
    if prelim_direction == "LONG" and closed_close < ma200_15m:
        gate_ma200 = False
        ma200_veto_reason = f"Cannot initiate LONG below 15M MA200 ({closed_close:.1f} < {ma200_15m:.1f})"
    elif prelim_direction == "SHORT" and closed_close > ma200_15m:
        gate_ma200 = False
        ma200_veto_reason = f"Cannot initiate SHORT above 15M MA200 ({closed_close:.1f} > {ma200_15m:.1f})"

# Calculate Candidate Entry & Stops
entry_pt = round(closed_close, 1)
stop_pt = 0.0
risk_pts = 0.0

if prelim_direction == "LONG":
    structural_stop = round(val_est - 1.0, 1) if val_est < entry_pt else round(entry_pt - MAX_RISK_PTS, 1)
    # Clamp to MAX_RISK_PTS
    if (entry_pt - structural_stop) > MAX_RISK_PTS:
        stop_pt = round(entry_pt - MAX_RISK_PTS, 1)
    else:
        stop_pt = structural_stop
    risk_pts = round(entry_pt - stop_pt, 2)
else:
    structural_stop = round(vah_est + 1.0, 1) if vah_est > entry_pt else round(entry_pt + MAX_RISK_PTS, 1)
    # Clamp to MAX_RISK_PTS
    if (structural_stop - entry_pt) > MAX_RISK_PTS:
        stop_pt = round(entry_pt + MAX_RISK_PTS, 1)
    else:
        stop_pt = structural_stop
    risk_pts = round(stop_pt - entry_pt, 2)

# VETO 2: Hard Risk Cap Filter (Absolute Safeguard)
gate_risk = (0 < risk_pts <= MAX_RISK_PTS)

# Unified Gate Status
all_gates_cleared = gate_delta and gate_cb and gate_ma200 and gate_risk

contracts = 10
action_card = []
settrade_block = ""

if all_gates_cleared and state["engine_t"].get("status") == "FLAT" and not is_pre_open_scan:
    total_risk_thb = risk_pts * 200 * contracts
    if prelim_direction == "LONG":
        t1_pt = round(entry_pt + 3.5, 1)
        t2_pt = round(entry_pt + 7.0, 1)
        action_card = [
            "🚨 <b>[ACTION REQUIRED: S50Z26 LONG ENTRY]</b>",
            f"• <b>Order:</b> <b>BUY S50Z26 @ {entry_pt:.1f}</b>",
            f"• <b>Hard Stop:</b> <code>{stop_pt:.1f}</code> (-{risk_pts:.1f} pts, Cap: $\\le 3.5$)",
            f"• <b>Position Size:</b> <b>{contracts} contracts</b> (Max Risk: <b>฿{total_risk_thb:,.0f} THB</b>)",
            f"• <b>15M MA200:</b> <code>{ma200_15m:.1f}</code> (Trend Confirmed 🟢)",
            f"• <b>T1 Bank (60% / 6 contracts):</b> Limit <b>{t1_pt:.1f}</b> (+3.5 pts >> Move stop to BE)",
            f"• <b>T2 Runner (40% / 4 contracts):</b> Target <b>{t2_pt:.1f}</b> or trail 15M EMA(9)",
            "• <b>⚡ Free-Roll Pyramiding:</b> On T1 hit, add <b>+2 contracts</b> on secondary base breakout."
        ]
        settrade_block = f"<code>OPEN LONG S50Z26 @ {entry_pt:.1f} | Vol: {contracts} | Stop: {stop_pt:.1f}</code>"
    else:
        t1_pt = round(entry_pt - 3.5, 1)
        t2_pt = round(entry_pt - 7.0, 1)
        action_card = [
            "🚨 <b>[ACTION REQUIRED: S50Z26 SHORT ENTRY]</b>",
            f"• <b>Order:</b> <b>SHORT S50Z26 @ {entry_pt:.1f}</b>",
            f"• <b>Hard Stop:</b> <code>{stop_pt:.1f}</code> (+{risk_pts:.1f} pts, Cap: $\\le 3.5$)",
            f"• <b>Position Size:</b> <b>{contracts} contracts</b> (Max Risk: <b>฿{total_risk_thb:,.0f} THB</b>)",
            f"• <b>15M MA200:</b> <code>{ma200_15m:.1f}</code> (Trend Confirmed 🔴)",
            f"• <b>T1 Bank (60% / 6 contracts):</b> Limit <b>{t1_pt:.1f}</b> (-3.5 pts >> Move stop to BE)",
            f"• <b>T2 Runner (40% / 4 contracts):</b> Target <b>{t2_pt:.1f}</b> or trail 15M EMA(9)",
            "• <b>⚡ Free-Roll Pyramiding:</b> On T1 hit, add <b>+2 contracts</b> on secondary base breakout."
        ]
        settrade_block = f"<code>OPEN SHORT S50Z26 @ {entry_pt:.1f} | Vol: {contracts} | Stop: {stop_pt:.1f}</code>"

# 4. State Updates & Trailing Logic
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

# 5. Build Telegram Status Card
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
        f"• <b>15M MA200:</b> <code>{ma200_15m:.2f} pts</code>\n"
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
    
    # Detail reason if gates are restricted
    reasons = []
    if not gate_delta: reasons.append(f"DELTA Decoupled ({delta_pct:+.2f}%)")
    if not gate_cb: reasons.append("Circuit Breaker Active")
    if not gate_ma200: reasons.append(ma200_veto_reason)
    if not gate_risk: reasons.append(f"Risk {risk_pts:.1f} pts > {MAX_RISK_PTS} cap")
    gate_reason_str = f" ({', '.join(reasons)})" if reasons else ""

    lines = [
        header_tag,
        f"📅 <b>Time:</b> <code>{html.escape(bkk_time_str)} BKK</code>",
        "━━━━━━━━━━━━━━━━━━",
        f"• <b>Closed 15M Price:</b> <code>{closed_close:.2f} pts</code> (Live: {s50_price:.2f})",
        f"• <b>15M MA200 Filter:</b> <code>{ma200_15m:.2f} pts</code>",
        f"• <b>Prior Day Value Area:</b> VAH: <code>{vah_est:.1f}</code> | POC: <code>{poc_est:.1f}</code> | VAL: <code>{val_est:.1f}</code>",
        f"• <b>DELTA Decoupler:</b> <code>{delta_pct:+.2f}%</code> ({delta_flag})",
        f"• <b>Pre-Flight 5-Gates:</b> {'🟢 ALL GATES CLEARED' if all_gates_cleared else '🔴 GATES RESTRICTED' + gate_reason_str}",
        f"• <b>Active State:</b> <b>{html.escape(t_state.get('status', 'FLAT'))}</b>",
        "━━━━━━━━━━━━━━━━━━"
    ]
    
    if action_card:
        lines.extend(action_card[1:])
        lines.append("━━━━━━━━━━━━━━━━━━")
    elif status == "FLAT":
        stand_aside_note = f"Standing aside. {gate_reason_str.lstrip(' (')}".rstrip(')') if gate_reason_str else "Standing aside. Waiting for structure confirmation."
        lines.extend([
            f"📋 <b>EXECUTION:</b> {stand_aside_note}",
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
