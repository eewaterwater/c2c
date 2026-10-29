#!/usr/bin/env python3
"""
Code2Cash Institutional Engine v7 - TFEX S50 15M Scanner
Engine T: Dual-Timeframe 15M Execution Engine

Core Protections & Invariants:
1. Hard Risk Cap: <= 3.5 pts maximum initial risk per trade (vetoes > 3.5 pts).
2. MA200 Trend Alignment: Longs strictly > MA200; Shorts strictly < MA200.
3. Execution Windows:
   - Morning Prep:    09:35 - 09:45 BKK (Auction Wick Limits Briefing)
   - Morning Active:  10:15 - 12:00 BKK (15M Closed-Bar Scanner)
   - Lunch Recess:    12:15 - 14:15 BKK (Blackout)
   - Afternoon:       14:15 - 16:10 BKK (15M Closed-Bar Scanner)
   - Post-Market:     >= 16:10 BKK (Settlement Hard Cutoff & Audit)
4. Stale-Bar / New-Day Guard: Rejects prior-day candles during morning evaluation.
5. Dynamic Support (ZOI): Price within +/- 1.5 pts of 15M EMA(20).
6. Bar Geometry: Lower rejection wick >= 0.2 pt for Longs; upper wick >= 0.2 pt for Shorts.
7. RSI(14) Momentum: 52.0 <= RSI <= 70.0 for Longs; 30.0 <= RSI <= 48.0 for Shorts.
"""

import os
import sys
import json
import html
import datetime
import pytz
import requests
import numpy as np
import pandas as pd
import yfinance as yf

# ==============================================================================
# CONFIGURATION & CONSTANTS
# ==============================================================================
BKK_TZ = pytz.timezone('Asia/Bangkok')

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MAX_RISK_PTS = 3.5          # Non-negotiable initial risk cap (Points)
POINT_VALUE_THB = 200.0     # SET50 Index Futures multiplier (200 THB/pt)
DEFAULT_CONTRACTS = 10      # Position sizing per 1R bracket
ZOI_TOLERANCE_PTS = 1.5     # Distance threshold from EMA(20)
MIN_WICK_PTS = 0.2          # Minimum wick rejection size
STATE_FILE = "state.json"

# ==============================================================================
# TIMING & BLACKOUT CHECK (LAYER I)
# ==============================================================================
def check_execution_window(now_dt: datetime.datetime) -> tuple[bool, str]:
    t = now_dt.time()

    # 1. Morning Auction Wick Prep Scan
    if datetime.time(9, 35) <= t < datetime.time(9, 45):
        return True, "WINDOW_AUCTION_PREP"

    # 2. Opening Auction Blackout
    if datetime.time(9, 45) <= t < datetime.time(10, 15):
        return False, "BLACKOUT_OPENING_AUCTION (09:45-10:15 BKK)"

    # 3. Midday Lunch Blackout
    if datetime.time(12, 15) <= t < datetime.time(14, 15):
        return False, "BLACKOUT_LUNCH_RECESS (12:15-14:15 BKK)"

    # 4. Post-Market / Settlement Cutoff
    if t >= datetime.time(16, 10) or t < datetime.time(9, 35):
        return False, "BLACKOUT_MARKET_CLOSED_OR_SETTLEMENT (>= 16:10 BKK)"

    # 5. Active Execution Windows
    if datetime.time(10, 15) <= t <= datetime.time(12, 0):
        return True, "WINDOW_MORNING_ACTIVE"

    if datetime.time(14, 15) <= t < datetime.time(16, 10):
        return True, "WINDOW_AFTERNOON_ACTIVE"

    return False, "NON_EXECUTION_INTERVAL"

# ==============================================================================
# TECHNICAL INDICATORS
# ==============================================================================
def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema_20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['ma_200'] = df['close'].rolling(window=200).mean()

    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss.replace(0, np.nan)
    df['rsi_14'] = 100 - (100 / (1 + rs))

    df['lower_wick'] = df[['open', 'close']].min(axis=1) - df['low']
    df['upper_wick'] = df['high'] - df[['open', 'close']].max(axis=1)
    df['body_size'] = (df['close'] - df['open']).abs()

    return df

# ==============================================================================
# TELEGRAM DISPATCHER
# ==============================================================================
def send_telegram_alert(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Missing Telegram credentials. Output:")
        print(message)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "📲 Open InnovestX", "url": "https://play.google.com/store/apps/details?id=com.scbs.easyinvest"},
                {"text": "📈 TradingView S50Z2026", "url": "https://www.tradingview.com/chart/?symbol=TFEX%3AS50Z2026"}
            ]
        ]
    }
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": keyboard
    }

    try:
        res = requests.post(url, json=payload, timeout=12)
        return res.status_code == 200
    except Exception as e:
        print(f"[ERROR] Failed to send Telegram alert: {e}")
        return False

# ==============================================================================
# DATA INGESTION HELPERS
# ==============================================================================
def fetch_clean_df(ticker_sym: str, period: str = "1mo", interval: str = "1d") -> pd.DataFrame:
    try:
        df = yf.download(ticker_sym, period=period, interval=interval, progress=False)
        if df is None or df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [str(c).lower() for c in df.columns]
        df = df.dropna(subset=["close", "high", "low", "open"])
        return df
    except Exception as e:
        print(f"[WARN] Fetch error for {ticker_sym} ({interval}): {e}")
        return pd.DataFrame()

# ==============================================================================
# STATE MACHINE MANAGEMENT
# ==============================================================================
def load_state(today_str: str) -> dict:
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
        except Exception as e:
            print(f"[WARN] State file read failed: {e}")

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
            "last_update": ""
        }

    if state["circuit_breakers"].get("date") != today_str:
        state["circuit_breakers"]["daily_stopouts"] = 0
        state["circuit_breakers"]["date"] = today_str

    return state

def save_state(state: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[ERROR] Failed to save state: {e}")

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================
def main():
    now_bkk = datetime.datetime.now(BKK_TZ)
    bkk_time_str = now_bkk.strftime("%Y-%m-%d %H:%M:%S")
    today_str = now_bkk.strftime("%Y-%m-%d")
    today_date = now_bkk.date()
    is_friday = (now_bkk.weekday() == 4)

    is_window_valid, window_status = check_execution_window(now_bkk)
    if not is_window_valid:
        print(f"[{now_bkk.strftime('%H:%M:%S')}] Gate 1 Veto: {window_status}")
        sys.exit(0)

    state = load_state(today_str)
    t_state = state["engine_t"]

    df_daily = fetch_clean_df("^SET50.BK", period="1mo", interval="1d")
    active_ticker = "^SET50.BK"
    if df_daily.empty:
        df_daily = fetch_clean_df("^SET50", period="1mo", interval="1d")
        active_ticker = "^SET50"

    if df_daily.empty:
        print(f"[{now_bkk.strftime('%H:%M:%S')}] Fatal: SET50 daily data unavailable.")
        sys.exit(0)

    s50_price = float(df_daily["close"].iloc[-1])
    prev_row = df_daily.iloc[-2] if len(df_daily) >= 2 else df_daily.iloc[-1]
    pdc = float(prev_row["close"])
    pdh = float(prev_row["high"])
    pdl = float(prev_row["low"])

    if pdh <= pdl or pdh == pdc:
        pdh = round(pdc + 4.20, 2)
        pdl = round(pdc - 4.80, 2)

    poc_est = round((pdh + pdl + pdc) / 3.0, 2)
    vah_est = pdh
    val_est = pdl

    delta_pct = 0.0
    try:
        df_delta = fetch_clean_df("DELTA.BK", period="5d", interval="1d")
        if len(df_delta) >= 2:
            d_curr = float(df_delta["close"].iloc[-1])
            d_prev = float(df_delta["close"].iloc[-2])
            if d_prev > 0:
                delta_pct = ((d_curr - d_prev) / d_prev) * 100.0
    except Exception as e:
        print(f"[WARN] DELTA check error: {e}")

    delta_flag = "⚠️ DECOUPLED" if abs(delta_pct) >= 1.5 else "✅ Normal"

    # Branch A: 09:35 Morning Auction Wick Prep
    if window_status == "WINDOW_AUCTION_PREP":
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
            f"🛡️ <b>Circuit Breakers:</b> Daily: <code>{state['circuit_breakers'].get('daily_stopouts', 0)}/2</code> | "
            f"Weekly: <code>{state['circuit_breakers'].get('weekly_stopouts', 0)}/3</code>\n"
            "<i>Code2Cash Institutional Engine v7</i>"
        )
        send_telegram_alert(msg)
        sys.exit(0)

    # Branch B: Active 15M Evaluation
    df_15m = fetch_clean_df("^SET50.BK", period="1mo", interval="15m")
    if df_15m.empty:
        df_15m = fetch_clean_df("^SET50", period="1mo", interval="15m")

    if len(df_15m) < 200:
        print(f"[{now_bkk.strftime('%H:%M:%S')}] Insufficient 15M bar history ({len(df_15m)}/200). Standing down.")
        sys.exit(0)

    df_15m = calculate_indicators(df_15m)

    closed_bar = df_15m.iloc[-2]
    bar_index = df_15m.index[-2]

    bar_dt = pd.to_datetime(bar_index)
    if bar_dt.tzinfo is None:
        bar_dt = BKK_TZ.localize(bar_dt)
    else:
        bar_dt = bar_dt.astimezone(BKK_TZ)

    if bar_dt.date() != today_date:
        print(f"[{now_bkk.strftime('%H:%M:%S')}] Gate 2 Veto: Stale bar date ({bar_dt.date()}) != today ({today_date})")
        sys.exit(0)

    close_p = float(closed_bar['close'])
    high_p = float(closed_bar['high'])
    low_p = float(closed_bar['low'])
    open_p = float(closed_bar['open'])
    ema_9 = float(closed_bar['ema_9'])
    ema_20 = float(closed_bar['ema_20'])
    ma_200 = float(closed_bar['ma_200'])
    rsi = float(closed_bar['rsi_14'])
    lower_wick = float(closed_bar['lower_wick'])
    upper_wick = float(closed_bar['upper_wick'])

    gate_delta = abs(delta_pct) <= 1.5
    gate_cb = state["circuit_breakers"].get("daily_stopouts", 0) < 2

    long_authorized = False
    short_authorized = False

    if close_p > ma_200 and abs(close_p - ema_20) <= ZOI_TOLERANCE_PTS:
        if lower_wick >= MIN_WICK_PTS and close_p >= open_p and (52.0 <= rsi <= 70.0):
            long_authorized = True

    if close_p < ma_200 and abs(close_p - ema_20) <= ZOI_TOLERANCE_PTS:
        if upper_wick >= MIN_WICK_PTS and close_p <= open_p and (30.0 <= rsi <= 48.0):
            short_authorized = True

    action_card = []
    settrade_block = ""
    status = t_state.get("status", "FLAT")

    if status == "FLAT" and gate_delta and gate_cb:
        if long_authorized:
            entry_price = round((open_p + close_p) / 2.0, 1)
            raw_stop = round(low_p - 0.2, 1)
            risk_pts = round(entry_price - raw_stop, 2)

            if 0.4 < risk_pts <= MAX_RISK_PTS:
                hard_stop = raw_stop
                t1_price = round(entry_price + 3.5, 1)
                t2_price = round(entry_price + 7.0, 1)
                risk_thb = int(risk_pts * POINT_VALUE_THB * DEFAULT_CONTRACTS)

                action_card = [
                    "🚨 <b>[ACTION REQUIRED: S50Z26 LONG ENTRY]</b>",
                    f"• <b>Order: BUY S50Z26 @ {entry_price:.1f}</b>",
                    f"• <b>Hard Stop: <code>{hard_stop:.1f}</code> (-{risk_pts:.1f} pts, Cap: $\\le {MAX_RISK_PTS}$)</b>",
                    f"• <b>Position Size:</b> {DEFAULT_CONTRACTS} contracts (Max Risk: <b>฿{risk_thb:,} THB</b>)",
                    f"• <b>15M MA200:</b> <code>{ma_200:.1f}</code> (Trend Confirmed 🟢)",
                    f"• <b>T1 Bank (60% / 6 contracts):</b> Limit <code>{t1_price:.1f}</code> (+3.5 pts >> Move stop to BE)",
                    f"• <b>T2 Runner (40% / 4 contracts):</b> Target <code>{t2_price:.1f}</code> or trail 15M EMA(9)",
                    "• <b>⚡ Free-Roll Pyramiding:</b> On T1 hit, add <b>+2 contracts</b> on secondary base breakout."
                ]
                settrade_block = f"<code>OPEN LONG S50Z26 @ {entry_price:.1f} | Vol: {DEFAULT_CONTRACTS} | Stop: {hard_stop:.1f}</code>"
            else:
                print(f"[{now_bkk.strftime('%H:%M:%S')}] Risk Gate Veto: Long stop {risk_pts} pts violates <= {MAX_RISK_PTS} cap.")

        elif short_authorized:
            entry_price = round((open_p + close_p) / 2.0, 1)
            raw_stop = round(high_p + 0.2, 1)
            risk_pts = round(raw_stop - entry_price, 2)

            if 0.4 < risk_pts <= MAX_RISK_PTS:
                hard_stop = raw_stop
                t1_price = round(entry_price - 3.5, 1)
                t2_price = round(entry_price - 7.0, 1)
                risk_thb = int(risk_pts * POINT_VALUE_THB * DEFAULT_CONTRACTS)

                action_card = [
                    "🚨 <b>[ACTION REQUIRED: S50Z26 SHORT ENTRY]</b>",
                    f"• <b>Order: SHORT S50Z26 @ {entry_price:.1f}</b>",
                    f"• <b>Hard Stop: <code>{hard_stop:.1f}</code> (+{risk_pts:.1f} pts, Cap: $\\le {MAX_RISK_PTS}$)</b>",
                    f"• <b>Position Size:</b> {DEFAULT_CONTRACTS} contracts (Max Risk: <b>฿{risk_thb:,} THB</b>)",
                    f"• <b>15M MA200:</b> <code>{ma_200:.1f}</code> (Trend Confirmed 🔴)",
                    f"• <b>T1 Bank (60% / 6 contracts):</b> Limit <code>{t1_price:.1f}</code> (-3.5 pts >> Move stop to BE)",
                    f"• <b>T2 Runner (40% / 4 contracts):</b> Target <code>{t2_price:.1f}</code> or trail 15M EMA(9)",
                    "• <b>⚡ Free-Roll Pyramiding:</b> On T1 hit, add <b>+2 contracts</b> on secondary base breakout."
                ]
                settrade_block = f"<code>OPEN SHORT S50Z26 @ {entry_price:.1f} | Vol: {DEFAULT_CONTRACTS} | Stop: {hard_stop:.1f}</code>"
            else:
                print(f"[{now_bkk.strftime('%H:%M:%S')}] Risk Gate Veto: Short stop {risk_pts} pts violates <= {MAX_RISK_PTS} cap.")

    alert_lines = []
    if status != "FLAT":
        pos_entry = t_state.get("entry_price", 0.0)
        direction = t_state.get("direction", "BUY")
        t1_val = t_state.get("t1_target", 0.0)
        stop_val = t_state.get("current_stop", 0.0)

        if status == "ACTIVE_INITIAL":
            t1_hit = (close_p >= t1_val) if direction == "BUY" else (close_p <= t1_val)
            if t1_hit:
                t_state["status"] = "T1_BANKED_BE"
                t_state["current_stop"] = pos_entry
                t_state["contracts_open"] = max(0, t_state.get("contracts_open", DEFAULT_CONTRACTS) - 4)
                pts_banked = abs(t1_val - pos_entry)
                t_state["t1_banked_pts"] = pts_banked
                t_state["pyramid_eligible"] = True

                alert_lines.append(f"🎯 <b>[TRANCHE 1 BANKED: +{pts_banked:.1f} PTS]</b>")
                alert_lines.append("• <b>Remaining Contracts:</b> Stop dragged to exact Breakeven (0.0 pt).")
                alert_lines.append("• <b>Pyramiding Status:</b> 🟢 AUTHORIZED (+2 contracts on secondary base).")

        stop_hit = (close_p <= stop_val) if direction == "BUY" else (close_p >= stop_val)
        if stop_hit:
            loss_pts = abs(pos_entry - stop_val) if status == "ACTIVE_INITIAL" else 0.0
            t_state["status"] = "FLAT"
            t_state["contracts_open"] = 0
            t_state["pyramid_eligible"] = False

            if status == "ACTIVE_INITIAL":
                state["circuit_breakers"]["daily_stopouts"] += 1
                state["circuit_breakers"]["weekly_stopouts"] += 1
                alert_lines.append(f"🛑 <b>[STOP-OUT HIT: -{loss_pts:.1f} PTS]</b>")
            else:
                alert_lines.append("🛡️ <b>[BREAKEVEN EXIT]</b> Runner tranche stopped out at 0.0 pt. Zero principal loss.")

    friday_flatten_warning = ""
    if is_friday and now_bkk.hour >= 16 and status == "ACTIVE_INITIAL":
        friday_flatten_warning = (
            "⚠️ <b>[FRIDAY 16:15 FLATTEN HOOK]</b>\n"
            "• Tranche 1 unhit before weekend recess.\n"
            "• Playbook Rule: <b>Flatten open contracts before 16:30 BKK</b> to eliminate unhedged weekend gap risk."
        )

    t_state["last_update"] = bkk_time_str
    state["engine_t"] = t_state
    save_state(state)

    header_tag = action_card[0] if action_card else "🇹🇭 <b>[C2C ENGINE T: S50Z26 TACTICAL MONITOR]</b>"

    lines = [
        header_tag,
        f"📅 <b>Time:</b> <code>{html.escape(bkk_time_str)} BKK</code>",
        "━━━━━━━━━━━━━━━━━━",
        f"• <b>Closed 15M Bar:</b> <code>{close_p:.2f} pts</code> (Live: {s50_price:.2f})",
        f"• <b>15M MA200:</b> <code>{ma_200:.2f} pts</code>",
        f"• <b>15M EMA(20) / ZOI:</b> <code>{ema_20:.2f} pts</code> (Dist: {abs(close_p - ema_20):.2f})",
        f"• <b>Prior Value Area:</b> VAH: <code>{vah_est:.1f}</code> | POC: <code>{poc_est:.1f}</code> | VAL: <code>{val_est:.1f}</code>",
        f"• <b>DELTA Decoupler:</b> <code>{delta_pct:+.2f}%</code> ({delta_flag})",
        f"• <b>Pre-Flight 5-Gates:</b> {'🟢 ALL GATES CLEARED' if action_card else '⚪ STANDING ASIDE (Filters Holding)'}",
        f"• <b>Active State:</b> <b>{html.escape(t_state.get('status', 'FLAT'))}</b>",
        "━━━━━━━━━━━━━━━━━━"
    ]

    if action_card:
        lines.extend(action_card[1:])
        lines.append("━━━━━━━━━━━━━━━━━━")

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

    full_msg = "\n".join(lines)
    send_telegram_alert(full_msg)
    print(f"[{now_bkk.strftime('%H:%M:%S')}] Engine T cycle completed successfully.")

if __name__ == "__main__":
    main()
