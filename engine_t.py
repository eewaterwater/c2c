#!/usr/bin/env python3
"""
Code2Cash Institutional Engine v7.2 - TFEX S50 15M Scanner
Engine T: Dual-Timeframe Execution & Session Monitor

Header Standards:
- Actionable: 🚨 [ACTION REQUIRED: S50Z26 ENTRY]
- Stand Aside: ⚪ [C2C ENGINE T: TACTICAL MONITOR - STANDING ASIDE]
- Pre-Open:   🌅 [C2C ENGINE T: 09:45 AUCTION WICK PREP]
"""

import os
import sys
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

MAX_RISK_PTS = 3.5           # Hard initial risk cap (Points)
POINT_VALUE_THB = 200.0      # SET50 Index Futures multiplier (200 THB/pt)
DEFAULT_CONTRACTS = 10       # Position sizing per 1R bracket
ZOI_TOLERANCE_PTS = 1.5      # Distance threshold from EMA(20)
MIN_WICK_PTS = 0.2           # Minimum wick rejection size
MAX_ACCEPTABLE_SLIP = 0.8    # Max difference between live tick and limit entry

# ==============================================================================
# TIMING & BLACKOUT CHECK (LAYER I)
# ==============================================================================
def check_execution_window(now_dt: datetime.datetime) -> tuple[bool, str]:
    t = now_dt.time()

    # 1. Morning Auction Wick Prep (09:35 - 09:45)
    if datetime.time(9, 35) <= t < datetime.time(9, 45):
        return True, "WINDOW_AUCTION_PREP"

    # 2. Opening Auction Blackout (09:45 - 10:15)
    if datetime.time(9, 45) <= t < datetime.time(10, 15):
        return False, "BLACKOUT_OPENING_AUCTION (09:45-10:15 BKK)"

    # 3. Midday Lunch Blackout (12:15 - 14:15)
    if datetime.time(12, 15) <= t < datetime.time(14, 15):
        return False, "BLACKOUT_LUNCH_RECESS (12:15-14:15 BKK)"

    # 4. Post-Market / Settlement Cutoff (>= 16:10 or before 09:35)
    if t >= datetime.time(16, 10) or t < datetime.time(9, 35):
        return False, "BLACKOUT_MARKET_CLOSED_OR_SETTLEMENT (>= 16:10 BKK)"

    # 5. Active Morning Window
    if datetime.time(10, 15) <= t <= datetime.time(12, 0):
        return True, "WINDOW_MORNING_ACTIVE"

    # 6. Active Afternoon Window
    if datetime.time(14, 15) <= t <= datetime.time(16, 10):
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
        print("[WARN] Missing Telegram credentials. Console print:\n", message)
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
# ALERT MESSAGE FORMATTERS
# ==============================================================================
def build_action_alert(
    direction: str,
    contract: str,
    entry_price: float,
    hard_stop: float,
    risk_pts: float,
    live_price: float,
    ma_200: float,
    ema_20: float,
    rsi: float,
    delta_decoupler: float,
    now_bkk: datetime.datetime
) -> str:
    if direction == "LONG":
        t1_price = round(entry_price + 3.5, 1)
        t2_price = round(entry_price + 7.0, 1)
        trend_status = f"BULLISH (Price {entry_price:.1f} > MA200 {ma_200:.1f}) 🟢"
        stop_sign = f"-{risk_pts:.1f}"
    else:
        t1_price = round(entry_price - 3.5, 1)
        t2_price = round(entry_price - 7.0, 1)
        trend_status = f"BEARISH (Price {entry_price:.1f} < MA200 {ma_200:.1f}) 🔴"
        stop_sign = f"+{risk_pts:.1f}"

    risk_thb = int(risk_pts * POINT_VALUE_THB * DEFAULT_CONTRACTS)
    zoi_dist = round(abs(entry_price - ema_20), 1)
    slip_diff = round(abs(live_price - entry_price), 1)
    slip_tag = f"Slip: {slip_diff:.1f} pt ✅" if slip_diff <= MAX_ACCEPTABLE_SLIP else f"⚠️ HIGH SLIP: {slip_diff:.1f} pts"

    return (
        f"🚨 <b>[ACTION REQUIRED: {contract} {direction} ENTRY]</b>\n"
        f"⏱ <i>{now_bkk.strftime('%Y-%m-%d %H:%M:%S')} BKK (15M MSS Confirmed)</i>\n\n"
        f"📋 <b>SETTRADE ONE-TAP EXECUTION:</b>\n"
        f"<code>OPEN {direction} {contract} @ {entry_price:.1f} | Vol: {DEFAULT_CONTRACTS} | Stop: {hard_stop:.1f}</code>\n\n"
        f"🎯 <b>BRACKET PROFILE ({DEFAULT_CONTRACTS} Contracts):</b>\n"
        f"• <b>Limit Entry:</b> <b>{entry_price:.1f}</b>\n"
        f"• <b>Hard Stop:</b> <code>{hard_stop:.1f}</code> ({stop_sign} pts | Max Risk: ฿{risk_thb:,} THB)\n"
        f"• <b>T1 Bank (60% / 6 cts):</b> Limit <b>{t1_price:.1f}</b> (±3.5 pts >> Move stop to BE)\n"
        f"• <b>T2 Runner (40% / 4 cts):</b> Target <b>{t2_price:.1f}</b> or trail 15M EMA(9)\n"
        f"• <b>⚡ Pyramiding:</b> On T1 hit, add +2 contracts on next 15M base breakout (Stop @ BE)\n\n"
        f"🛡 <b>GATE SANITY AUDIT:</b>\n"
        f"• Regime: {trend_status}\n"
        f"• Dynamic ZOI: {zoi_dist:.1f} pts to EMA(20) [Target: ≤ 1.5] 🟢\n"
        f"• 15M RSI(14): {rsi:.1f} 🟢\n"
        f"• DELTA Movement: {delta_decoupler:+.2f}% 🟢\n"
        f"• Live Tick: {live_price:.1f} ({slip_tag})\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Code2Cash Institutional Engine v7.2</i>"
    )

def build_monitor_alert(
    contract: str,
    live_price: float,
    closed_price: float,
    ma_200: float,
    ema_20: float,
    rsi: float,
    delta_decoupler: float,
    veto_reason: str,
    now_bkk: datetime.datetime
) -> str:
    trend_tag = "🟢 Above MA200" if closed_price >= ma_200 else "🔴 Below MA200"
    delta_flag = "⚠️ DECOUPLED" if abs(delta_decoupler) > 1.5 else "✅ Normal"

    return (
        f"⚪ <b>[C2C ENGINE T: TACTICAL MONITOR - STANDING ASIDE]</b>\n"
        f"⏱ <i>{now_bkk.strftime('%Y-%m-%d %H:%M:%S')} BKK</i>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Contract:</b> <code>{contract}</code>\n"
        f"• <b>Live Price:</b> <code>{live_price:.1f}</code> (15M Closed: {closed_price:.1f})\n"
        f"• <b>Trend Alignment:</b> {trend_tag} (MA200: <code>{ma_200:.1f}</code>)\n"
        f"• <b>15M EMA(20) / ZOI:</b> <code>{ema_20:.1f}</code> (Dist: {abs(closed_price - ema_20):.1f} pts)\n"
        f"• <b>15M RSI(14):</b> <code>{rsi:.1f}</code>\n"
        f"• <b>DELTA Decoupler:</b> <code>{delta_decoupler:+.2f}%</code> ({delta_flag})\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>EXECUTION:</b> <b>Standing Aside.</b>\n"
        f"• Filter Status: <i>{veto_reason}</i>\n"
        "• Capital preserved in Cash Hold.\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Code2Cash Institutional Engine v7.2</i>"
    )

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
        return df.dropna(subset=["close", "high", "low", "open"])
    except Exception as e:
        print(f"[WARN] Fetch error for {ticker_sym} ({interval}): {e}")
        return pd.DataFrame()

# ==============================================================================
# MAIN SCANNER & EVALUATION PIPELINE
# ==============================================================================
def main():
    now_bkk = datetime.datetime.now(BKK_TZ)
    bkk_time_str = now_bkk.strftime("%Y-%m-%d %H:%M:%S")
    today_date = now_bkk.date()

    # Gate 1: Check Execution Window
    is_valid_window, window_status = check_execution_window(now_bkk)
    if not is_valid_window:
        print(f"[{now_bkk.strftime('%H:%M:%S')}] Gate 1 Veto: {window_status}")
        return

    # 1. Fetch Daily Underlyings
    df_daily = fetch_clean_df("^SET50.BK", period="1mo", interval="1d")
    active_ticker = "^SET50.BK"
    if df_daily.empty:
        df_daily = fetch_clean_df("^SET50", period="1mo", interval="1d")
        active_ticker = "^SET50"

    pdc, pdh, pdl = 0.0, 0.0, 0.0
    if not df_daily.empty:
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

    # 2. Check DELTA Decoupler
    delta_pct = 0.0
    try:
        df_del = fetch_clean_df("DELTA.BK", period="5d", interval="1d")
        if len(df_del) >= 2:
            delta_pct = ((float(df_del["close"].iloc[-1]) - float(df_del["close"].iloc[-2])) / float(df_del["close"].iloc[-2])) * 100.0
    except Exception as e:
        print(f"[WARN] Failed fetching DELTA.BK: {e}")

    # ==========================================================================
    # BRANCH A: 09:35 PRE-OPEN AUCTION WICK PREP
    # ==========================================================================
    if window_status == "WINDOW_AUCTION_PREP":
        delta_flag = "⚠️ DECOUPLED" if abs(delta_pct) >= 1.5 else "✅ Normal"
        auction_msg = (
            "🌅 <b>[C2C ENGINE T: 09:45 AUCTION WICK PREP]</b>\n"
            f"⏱ <i>{bkk_time_str} BKK</i>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Contract:</b> <code>S50Z26 (Index: {active_ticker})</code>\n"
            f"• <b>Prior Close (PDC):</b> <code>{pdc:.2f} pts</code>\n"
            f"• <b>Prior High (VAH):</b> <code>{vah_est:.2f} pts</code>\n"
            f"• <b>Prior Low (VAL):</b> <code>{val_est:.2f} pts</code>\n"
            f"• <b>Estimated POC:</b> <code>{poc_est:.2f} pts</code>\n"
            f"• <b>DELTA Decoupler:</b> <code>{delta_pct:+.2f}%</code> ({delta_flag})\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🎯 <b>AUCTION WICK LIMIT LEVELS (09:45–10:00 BKK):</b>\n"
            f"• <b>Long Wick Fade:</b> Limit Buy at <b>{val_est:.1f}</b> | Stop: {val_est - 2.5:.1f} (-2.5 pts)\n"
            f"• <b>Short Wick Fade:</b> Limit Short at <b>{vah_est:.1f}</b> | Stop: {vah_est + 2.5:.1f} (+2.5 pts)\n"
            "• <b>Rule:</b> Target +2.0 to +3.0 pts bounce. <b>At 09:58 BKK, bank or pull stop to BE before cash open!</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>Code2Cash Institutional Engine v7.2</i>"
        )
        send_telegram_alert(auction_msg)
        return

    # ==========================================================================
    # BRANCH B: ACTIVE 15M EXECUTION & MONITORING WINDOWS
    # ==========================================================================
    df_15m = fetch_clean_df("^SET50.BK", period="1mo", interval="15m")
    if df_15m.empty:
        df_15m = fetch_clean_df("^SET50", period="1mo", interval="15m")

    if len(df_15m) < 200:
        print(f"[{now_bkk.strftime('%H:%M:%S')}] Insufficient 15M bars ({len(df_15m)}/200).")
        return

    df_15m = calculate_indicators(df_15m)

    # Live tick vs Closed bar (iloc[-2] is verified closed bar)
    live_price = float(df_15m["close"].iloc[-1])
    closed_bar = df_1
