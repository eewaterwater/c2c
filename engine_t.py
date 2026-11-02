#!/usr/bin/env python3
"""
Code2Cash Institutional Engine v7.2 - TFEX S50 15M Scanner
Engine T: Dual-Timeframe Execution & Session Monitor
"""

import os
import sys
import datetime
import pytz
import requests
import numpy as np
import pandas as pd
import yfinance as yf

BKK_TZ = pytz.timezone('Asia/Bangkok')
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MAX_RISK_PTS = 3.5
POINT_VALUE_THB = 200.0
DEFAULT_CONTRACTS = 10
ZOI_TOLERANCE_PTS = 1.5
MIN_WICK_PTS = 0.2
MAX_ACCEPTABLE_SLIP = 0.8

def check_execution_window(now_dt: datetime.datetime) -> tuple[bool, str]:
    t = now_dt.time()
    if datetime.time(9, 35) <= t < datetime.time(9, 45):
        return True, "WINDOW_AUCTION_PREP"
    if datetime.time(9, 45) <= t < datetime.time(10, 15):
        return False, "BLACKOUT_OPENING_AUCTION (09:45-10:15 BKK)"
    if datetime.time(12, 15) <= t < datetime.time(14, 15):
        return False, "BLACKOUT_LUNCH_RECESS (12:15-14:15 BKK)"
    if t >= datetime.time(16, 10) or t < datetime.time(9, 35):
        return False, "BLACKOUT_MARKET_CLOSED_OR_SETTLEMENT (>= 16:10 BKK)"
    if datetime.time(10, 15) <= t <= datetime.time(12, 0):
        return True, "WINDOW_MORNING_ACTIVE"
    if datetime.time(14, 15) <= t <= datetime.time(16, 10):
        return True, "WINDOW_AFTERNOON_ACTIVE"
    return False, "NON_EXECUTION_INTERVAL"

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
        print(f"Telegram status: {res.status_code}")
        return res.status_code == 200
    except Exception as e:
        print(f"[ERROR] Failed to send Telegram alert: {e}")
        return False

def build_action_alert(direction, contract, entry_price, hard_stop, risk_pts, live_price, ma_200, ema_20, rsi, delta_decoupler, now_bkk):
    t1_price = round(entry_price + 3.5, 1) if direction == "LONG" else round(entry_price - 3.5, 1)
    t2_price = round(entry_price + 7.0, 1) if direction == "LONG" else round(entry_price - 7.0, 1)
    trend_status = f"BULLISH (Price {entry_price:.1f} > MA200 {ma_200:.1f}) 🟢" if direction == "LONG" else f"BEARISH (Price {entry_price:.1f} < MA200 {ma_200:.1f}) 🔴"
    stop_sign = f"-{risk_pts:.1f}" if direction == "LONG" else f"+{risk_pts:.1f}"
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

def build_monitor_alert(contract, live_price, closed_price, ma_200, ema_20, rsi, delta_decoupler, veto_reason, now_bkk):
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
        f"• Status: <i>{veto_reason}</i>\n"
        "• Capital preserved in Cash Hold.\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Code2Cash Institutional Engine v7.2</i>"
    )

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
        print(f"[WARN] Fetch error {ticker_sym}: {e}")
        return pd.DataFrame()

def main():
    now_bkk = datetime.datetime.now(BKK_TZ)
    bkk_time_str = now_bkk.strftime("%Y-%m-%d %H:%M:%S")
    is_manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

    is_valid_window, window_status = check_execution_window(now_bkk)
    if not is_valid_window and not is_manual:
        print(f"Gate 1 Veto: {window_status}")
        return

    # 1. Fetch Underlyings
    df_daily = fetch_clean_df("^SET50.BK", period="1mo", interval="1d")
    active_ticker = "^SET50.BK"
    if df_daily.empty:
        df_daily = fetch_clean_df("^SET50", period="1mo", interval="1d")
        active_ticker = "^SET50"

    pdc, pdh, pdl = 0.0, 0.0, 0.0
    if not df_daily.empty:
        prev_row = df_daily.iloc[-2] if len(df_daily) >= 2 else df_daily.iloc[-1]
        pdc, pdh, pdl = float(prev_row["close"]), float(prev_row["high"]), float(prev_row["low"])
    poc_est = round((pdh + pdl + pdc) / 3.0, 2)
    vah_est, val_est = pdh, pdl

    # 2. DELTA Decoupler
    delta_pct = 0.0
    try:
        df_del = fetch_clean_df("DELTA.BK", period="5d", interval="1d")
        if len(df_del) >= 2:
            delta_pct = ((float(df_del["close"].iloc[-1]) - float(df_del["close"].iloc[-2])) / float(df_del["close"].iloc[-2])) * 100.0
    except Exception: pass

    # 3. 15M Data
    df_15m = fetch_clean_df("^SET50.BK", period="1mo", interval="15m")
    if df_15m.empty:
        df_15m = fetch_clean_df("^SET50", period="1mo", interval="15m")

    if len(df_15m) < 20:
        msg = f"⚪ <b>[C2C ENGINE T: STANDING ASIDE]</b>\n⏱ <i>{bkk_time_str} BKK</i>\n• SET50 15M feed initializing ({len(df_15m)} bars available).\n• Cash Hold active."
        send_telegram_alert(msg)
        return

    df_15m = calculate_indicators(df_15m)
    live_price = float(df_15m["close"].iloc[-1])
    closed_bar = df_15m.iloc[-2] if len(df_15m) >= 2 else df_15m.iloc[-1]

    close_p = float(closed_bar['close'])
    high_p = float(closed_bar['high'])
    low_p = float(closed_bar['low'])
    open_p = float(closed_bar['open'])
    ema_20 = float(closed_bar['ema_20']) if not np.isnan(closed_bar['ema_20']) else close_p
    ma_200 = float(closed_bar['ma_200']) if not np.isnan(closed_bar['ma_200']) else float(df_15m['close'].mean())
    rsi = float(closed_bar['rsi_14']) if not np.isnan(closed_bar['rsi_14']) else 50.0
    lower_wick = float(closed_bar['lower_wick'])
    upper_wick = float(closed_bar['upper_wick'])

    if abs(delta_pct) > 1.5:
        veto_reason = f"DELTA Decoupled ({delta_pct:+.2f}%) exceeds ±1.5% threshold."
        send_telegram_alert(build_monitor_alert("S50Z26", live_price, close_p, ma_200, ema_20, rsi, delta_pct, veto_reason, now_bkk))
        return

    # Check Long
    if close_p > ma_200 and abs(close_p - ema_20) <= ZOI_TOLERANCE_PTS:
        if lower_wick >= MIN_WICK_PTS and close_p >= open_p and (52.0 <= rsi <= 70.0):
            entry_price = round((open_p + close_p) / 2.0, 1)
            raw_stop = round(low_p - 0.2, 1)
            risk_pts = round(entry_price - raw_stop, 2)
            if 0.4 < risk_pts <= MAX_RISK_PTS:
                send_telegram_alert(build_action_alert("LONG", "S50Z26", entry_price, raw_stop, risk_pts, live_price, ma_200, ema_20, rsi, delta_pct, now_bkk))
                return
            else:
                veto_reason = f"Long stop ({risk_pts:.1f} pts) violates ≤ {MAX_RISK_PTS} pt cap."
                send_telegram_alert(build_monitor_alert("S50Z26", live_price, close_p, ma_200, ema_20, rsi, delta_pct, veto_reason, now_bkk))
                return

    # Check Short
    if close_p < ma_200 and abs(close_p - ema_20) <= ZOI_TOLERANCE_PTS:
        if upper_wick >= MIN_WICK_PTS and close_p <= open_p and (30.0 <= rsi <= 48.0):
            entry_price = round((open_p + close_p) / 2.0, 1)
            raw_stop = round(high_p + 0.2, 1)
            risk_pts = round(raw_stop - entry_price, 2)
            if 0.4 < risk_pts <= MAX_RISK_PTS:
                send_telegram_alert(build_action_alert("SHORT", "S50Z26", entry_price, raw_stop, risk_pts, live_price, ma_200, ema_20, rsi, delta_pct, now_bkk))
                return
            else:
                veto_reason = f"Short stop ({risk_pts:.1f} pts) violates ≤ {MAX_RISK_PTS} pt cap."
                send_telegram_alert(build_monitor_alert("S50Z26", live_price, close_p, ma_200, ema_20, rsi, delta_pct, veto_reason, now_bkk))
                return

    # Stand-Aside Dispatch
    dist_zoi = abs(close_p - ema_20)
    if dist_zoi > ZOI_TOLERANCE_PTS:
        veto_reason = f"Price extended {dist
