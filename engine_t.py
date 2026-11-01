#!/usr/bin/env python3
"""
Code2Cash Institutional Engine v7.1 - TFEX S50 15M Scanner
Engine T: Dual-Timeframe 15M Execution Engine

Key Production Guardrails:
1. Hard Risk Cap: <= 3.5 pts maximum initial risk per trade (strictly rejects > 3.5 pts).
2. MA200 Trend Alignment: Longs strictly > MA200; Shorts strictly < MA200.
3. Strict Blackout Windows:
   - 09:45 - 10:15 BKK (Opening Auction)
   - 12:15 - 14:15 BKK (Midday Lunch Recess)
   - >= 16:10 BKK (Closing Settlement / Post-Market)
4. Calendar-Date Stale-Bar Guard: Discards bars from previous calendar dates.
5. Dynamic ZOI Tolerance: Price within +/- 1.5 pts of 15M EMA(20).
6. Bar Geometry: Lower rejection wick >= 0.2 pt for Longs; upper wick >= 0.2 pt for Shorts.
7. RSI(14) Momentum: 52.0 <= RSI <= 70.0 for Longs; 30.0 <= RSI <= 48.0 for Shorts.
8. One-Tap Settrade Execution Box with Live Slip Monitoring.
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

# Telegram Bot Config (Reads from server environment variables or manual fallback)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Playbook Parameters
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

    # 1. Opening Auction Blackout (09:45 - 10:15)
    if datetime.time(9, 45) <= t < datetime.time(10, 15):
        return False, "BLACKOUT_OPENING_AUCTION (09:45-10:15 BKK)"

    # 2. Midday Lunch Blackout (12:15 - 14:15)
    if datetime.time(12, 15) <= t < datetime.time(14, 15):
        return False, "BLACKOUT_LUNCH_RECESS (12:15-14:15 BKK)"

    # 3. Post-Market / Settlement Cutoff (>= 16:10 or before 09:45)
    if t >= datetime.time(16, 10) or t < datetime.time(9, 45):
        return False, "BLACKOUT_MARKET_CLOSED_OR_SETTLEMENT (>= 16:10 BKK)"

    # 4. Active Morning Window
    if datetime.time(10, 15) <= t <= datetime.time(12, 0):
        return True, "WINDOW_MORNING_ACTIVE"

    # 5. Active Afternoon Window
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
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        print(f"[ERROR] Failed to send Telegram alert: {e}")
        return False

# ==============================================================================
# ALERT MESSAGE FORMATTER
# ==============================================================================
def build_telegram_alert(
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
        regime_icon = "🟢 LONG"
        stop_sign = f"-{risk_pts:.1f}"
    else:
        t1_price = round(entry_price - 3.5, 1)
        t2_price = round(entry_price - 7.0, 1)
        trend_status = f"BEARISH (Price {entry_price:.1f} < MA200 {ma_200:.1f}) 🔴"
        regime_icon = "🔴 SHORT"
        stop_sign = f"+{risk_pts:.1f}"

    risk_thb = int(risk_pts * POINT_VALUE_THB * DEFAULT_CONTRACTS)
    zoi_dist = round(abs(entry_price - ema_20), 1)
    slip_diff = round(abs(live_price - entry_price), 1)
    slip_tag = f"Slip: {slip_diff:.1f} pt ✅" if slip_diff <= MAX_ACCEPTABLE_SLIP else f"⚠️ HIGH SLIP: {slip_diff:.1f} pts"

    return (
        f"🚨 <b>[ENGINE T] {regime_icon} {contract} @ {entry_price:.1f}</b>\n"
        f"⏱ <i>{now_bkk.strftime('%Y-%m-%d %H:%M:%S')} BKK (15M Confirmed)</i>\n\n"
        f"📋 <b>SETTRADE ONE-TAP EXECUTION:</b>\n"
        f"<code>OPEN {direction} {contract} @ {entry_price:.1f} | Vol: {DEFAULT_CONTRACTS} | Stop: {hard_stop:.1f}</code>\n\n"
        f"🎯 <b>BRACKET PROFILE ({DEFAULT_CONTRACTS} Contracts):</b>\n"
        f"• <b>Limit Entry:</b> {entry_price:.1f}\n"
        f"• <b>Hard Stop:</b> {hard_stop:.1f} ({stop_sign} pts | Max Risk: ฿{risk_thb:,} THB)\n"
        f"• <b>T1 Bank (60% / 6 cts):</b> Limit {t1_price:.1f} (±3.5 pts >> Move stop to BE)\n"
        f"• <b>T2 Runner (40% / 4 cts):</b> Trail 15M EMA(9) (Target: {t2_price:.1f})\n\n"
        f"🛡 <b>GATE SANITY AUDIT:</b>\n"
        f"• Regime: {trend_status}\n"
        f"• Dynamic ZOI: {zoi_dist:.1f} pts to EMA(20) [Target: ≤ 1.5] 🟢\n"
        f"• 15M RSI(14): {rsi:.1f} 🟢\n"
        f"• DELTA Decoupler: {delta_decoupler:.2f}% 🟢\n"
        f"• Live Price: {live_price:.1f} ({slip_tag})\n"
    )

# ==============================================================================
# 5-GATE ENGINE T EVALUATION PIPELINE
# ==============================================================================
def evaluate_engine_t(df: pd.DataFrame, live_price: float, delta_decoupler: float, contract: str = "S50Z26"):
    now_bkk = datetime.datetime.now(BKK_TZ)
    today_date = now_bkk.date()

    # Gate 1: Timing & Blackout
    is_window_valid, window_status = check_execution_window(now_bkk)
    if not is_window_valid:
        print(f"[{now_bkk.strftime('%H:%M:%S')}] Gate 1 Veto: {window_status}")
        return

    if len(df) < 200:
        print(f"[{now_bkk.strftime('%H:%M:%S')}] Insufficient bar history ({len(df)}/200).")
        return

    df = calculate_indicators(df)
    bar = df.iloc[-1]

    # Gate 2: Stale Bar / Date Guard (Prevent prior-day closing ghost signals)
    if 'timestamp' in bar:
        bar_dt = pd.to_datetime(bar['timestamp'])
    else:
        bar_dt = pd.to_datetime(bar.name)

    if bar_dt.tzinfo is None:
        bar_dt = BKK_TZ.localize(bar_dt)
    else:
        bar_dt = bar_dt.astimezone(BKK_TZ)

    if bar_dt.date() != today_date:
        print(f"[{now_bkk.strftime('%H:%M:%S')}] Gate 2 Veto: Stale bar date ({bar_dt.date()}) != today ({today_date})")
        return

    close = float(bar['close'])
    high = float(bar['high'])
    low = float(bar['low'])
    open_p = float(bar['open'])
    ema_20 = float(bar['ema_20'])
    ma_200 = float(bar['ma_200'])
    rsi = float(bar['rsi_14'])
    lower_wick = float(bar['lower_wick'])
    upper_wick = float(bar['upper_wick'])

    # Gate 3: DELTA Decoupler Check
    if abs(delta_decoupler) > 1.5:
        print(f"[{now_bkk.strftime('%H:%M:%S')}] Gate 3 Veto: DELTA decoupled ({delta_decoupler:.2f}%)")
        return

    # --- LONG EVALUATION ---
    if close > ma_200 and abs(close - ema_20) <= ZOI_TOLERANCE_PTS:
        if lower_wick >= MIN_WICK_PTS and close >= open_p and (52.0 <= rsi <= 70.0):
            entry_price = round((open_p + close) / 2.0, 1)
            raw_stop = round(low - 0.2, 1)
            risk_pts = round(entry_price - raw_stop, 2)

            # Non-Negotiable Risk Cap Veto
            if risk_pts > MAX_RISK_PTS:
                print(f"[{now_bkk.strftime('%H:%M:%S')}] Risk Veto: Long stop ({risk_pts} pts) > {MAX_RISK_PTS} pt limit.")
                return
            if risk_pts <= 0.4:
                return

            alert_msg = build_telegram_alert(
                direction="LONG",
                contract=contract,
                entry_price=entry_price,
                hard_stop=raw_stop,
                risk_pts=risk_pts,
                live_price=live_price,
                ma_200=ma_200,
                ema_20=ema_20,
                rsi=rsi,
                delta_decoupler=delta_decoupler,
                now_bkk=now_bkk
            )
            send_telegram_alert(alert_msg)
            return

    # --- SHORT EVALUATION ---
    if close < ma_200 and abs(close - ema_20) <= ZOI_TOLERANCE_PTS:
        if upper_wick >= MIN_WICK_PTS and close <= open_p and (30.0 <= rsi <= 48.0):
            entry_price = round((open_p + close) / 2.0, 1)
            raw_stop = round(high + 0.2, 1)
            risk_pts = round(raw_stop - entry_price, 2)

            # Non-Negotiable Risk Cap Veto
            if risk_pts > MAX_RISK_PTS:
                print(f"[{now_bkk.strftime('%H:%M:%S')}] Risk Veto: Short stop ({risk_pts} pts) > {MAX_RISK_PTS} pt limit.")
                return
            if risk_pts <= 0.4:
                return

            alert_msg = build_telegram_alert(
                direction="SHORT",
                contract=contract,
                entry_price=entry_price,
                hard_stop=raw_stop,
                risk_pts=risk_pts,
                live_price=live_price,
                ma_200=ma_200,
                ema_20=ema_20,
                rsi=rsi,
                delta_decoupler=delta_decoupler,
                now_bkk=now_bkk
            )
            send_telegram_alert(alert_msg)
            return

    print(f"[{now_bkk.strftime('%H:%M:%S')}] Engine T: No valid setup. Tape in Cash Hold.")

# ==============================================================================
# LIVE DATA LOADER & ENTRY POINT
# ==============================================================================
def main():
    now_bkk = datetime.datetime.now(BKK_TZ)

    # Pre-check execution boundaries before fetching network data
    is_valid_window, window_status = check_execution_window(now_bkk)
    if not is_valid_window:
        print(f"[{now_bkk.strftime('%H:%M:%S')}] Gate 1 Veto: {window_status}")
        return

    # 1. Fetch 15M SET50 Data
    df_15m = pd.DataFrame()
    for sym in ["^SET50.BK", "^SET50"]:
        try:
            d = yf.download(sym, period="1mo", interval="15m", progress=False)
            if d is not None and not d.empty:
                if isinstance(d.columns, pd.MultiIndex):
                    d.columns = d.columns.get_level_values(0)
                d.columns = [str(c).lower() for c in d.columns]
                df_15m = d.dropna(subset=["close", "high", "low", "open"])
                if len(df_15m) >= 200:
                    break
        except Exception as e:
            print(f"[WARN] Failed fetching {sym}: {e}")

    if df_15m.empty or len(df_15m) < 200:
        print(f"[{now_bkk.strftime('%H:%M:%S')}] Fatal: Insufficient 15M bars ({len(df_15m)}/200).")
        return

    # Bar Completion Check: Evaluate strictly on closed bar (iloc[-2]); iloc[-1] is live ticking bar
    live_price = float(df_15m["close"].iloc[-1])
    closed_bars_df = df_15m.iloc[:-1].copy()

    # 2. Fetch DELTA Movement for Decoupler Check
    delta_pct = 0.0
    try:
        df_del = yf.download("DELTA.BK", period="5d", interval="1d", progress=False)
        if df_del is not None and not df_del.empty:
            if isinstance(df_del.columns, pd.MultiIndex):
                df_del.columns = df_del.columns.get_level_values(0)
            c_del = df_del["Close"].dropna()
            if len(c_del) >= 2:
                delta_pct = ((float(c_del.iloc[-1]) - float(c_del.iloc[-2])) / float(c_del.iloc[-2])) * 100.0
    except Exception as e:
        print(f"[WARN] Failed fetching DELTA.BK: {e}")

    # 3. Trigger 5-Gate Evaluation
    evaluate_engine_t(
        df=closed_bars_df,
        live_price=live_price,
        delta_decoupler=delta_pct,
        contract="S50Z26"
    )

if __name__ == "__main__":
    main()
