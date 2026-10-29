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
            "🎯 <b>S50Z26 AUCTION WICK LIMITS (09:45–1
           
