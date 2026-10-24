import os
import sys
import requests
from datetime import datetime
import pytz
import exchange_calendars as xcals
import yfinance as yf
import pandas as pd
import numpy as np

token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("TELEGRAM_CHAT_ID")
event_name = os.environ.get("EVENT_NAME", "workflow_dispatch")
bkk_now = datetime.now(pytz.timezone("Asia/Bangkok"))
bkk_date_str = bkk_now.strftime("%Y-%m-%d")
bkk_time_str = bkk_now.strftime("%Y-%m-%d %H:%M:%S")

# 0. SET / TFEX Market Calendar Check
xbkk = xcals.get_calendar("XBKK")
is_market_open_today = xbkk.is_session(bkk_date_str)

if not is_market_open_today and event_name == "schedule":
    msg = (
        f"⏸️ <b>[C2C Engine T - Market Closed]</b>\n"
        f"📅 <b>Date:</b> {bkk_date_str} (BKK)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Status:</b> SET / TFEX is closed today (Holiday/Weekend).\n"
        f"• <b>Action:</b> Standing down. No scan executed.\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>Automated pipeline running via GitHub Cloud.</i>"
    )
    if token and chat_id:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML", "disable_notification": True},
            timeout=15
        )
    sys.exit(0)

notice_header = ""
if not is_market_open_today:
    notice_header = "⚠️ <b>[TEST MODE: SET Closed Today - Using Prior Session Data]</b>\n"

def safe_get_series(ticker, period="5d", interval="1d"):
    try:
        df = yf.download(ticker, period=period, interval=interval, progress=False)
        if df is not None and not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                return df["Close"].iloc[:, 0].dropna().values
            return df["Close"].dropna().values
    except Exception as e:
        print(f"Warning {ticker}: {e}")
    return np.array([])

# 1. Asian E-mini S&P (ES=F)
es_veto = False
es_pct = 0.0
c_es = safe_get_series("ES=F", "5d", "1d")
if len(c_es) >= 2:
    es_pct = float(((c_es[-1] - c_es[-2]) / c_es[-2]) * 100.0)
    if es_pct <= -0.60:
        es_veto = True

# 2. USD/THB FX Gate (5-Day Trend Slope)
thb_veto = False
thb_chg = 0.0
c_thb = safe_get_series("THB=X", "10d", "1d")
if len(c_thb) >= 5:
    thb_chg = float(((c_thb[-1] - c_thb[-5]) / c_thb[-5]) * 100.0)
    if thb_chg > 0.30:
        thb_veto = True

# 3. Breadth & DELTA Decoupler
tickers = ["DELTA.BK", "PTT.BK", "PTTEP.BK", "BDMS.BK", "CPALL.BK"]
advancing = 0
breadth_details = []
delta_chg = 0.0

for t in tickers:
    clean = t.replace(".BK", "")
    c_t = safe_get_series(t, "5d", "1d")
    if len(c_t) >= 2:
        chg = float(((c_t[-1] - c_t[-2]) / c_t[-2]) * 100.0)
        if clean == "DELTA":
            delta_chg = chg
        if chg > 0:
            advancing += 1
            breadth_details.append(f"🟢 {clean}: {chg:+.2f}%")
        elif chg < 0:
            breadth_details.append(f"🔴 {clean}: {chg:+.2f}%")
        else:
            breadth_details.append(f"⚪ {clean}: 0.00%")
    else:
        breadth_details.append(f"⚪ {clean}: Flat")

breadth_pass = (advancing >= 3)
delta_veto = (delta_chg < -1.50)

# 4. Multi-Timeframe Structure (Daily Trend + 15M MSS Trigger)
s50_price = 930.0
s50_daily_ema20 = 925.0
s50_15m_ema20 = 928.0
s50_rsi15m = 50.0
daily_bullish = True
mss_15m_pass = True

# Daily Trend
c_s50_d = safe_get_series("^SET50", "3mo", "1d")
if len(c_s50_d) >= 20:
    s50_price = float(c_s50_d[-1])
    s_ema_d = pd.Series(c_s50_d).ewm(span=20, adjust=False).mean()
    s50_daily_ema20 = float(s_ema_d.iloc[-1])
    daily_bullish = s50_price >= s50_daily_ema20

# 15M Market Structure Shift Check
try:
    df_15m = yf.download("^SET50", period="5d", interval="15m", progress=False)
    if df_15m is not None and not df_15m.empty:
        c_15m = df_15m["Close"].iloc[:, 0].dropna() if isinstance(df_15m.columns, pd.MultiIndex) else df_15m["Close"].dropna()
        if len(c_15m) >= 25:
            s50_price = float(c_15m.iloc[-1])
            ema20_15m_series = c_15m.ewm(span=20, adjust=False).mean()
            s50_15m_ema20 = float(ema20_15m_series.iloc[-1])

            # 14-period RSI
            delta_p = c_15m.diff()
            gain = (delta_p.where(delta_p > 0, 0)).rolling(14).mean()
            loss = (-delta_p.where(delta_p < 0, 0)).rolling(14).mean()
            rs = gain / (loss + 1e-9)
            s50_rsi15m = float((100 - (100 / (1 + rs))).iloc[-1])

            # 15M MSS Trigger Rule: Price > 15M EMA20 and RSI > 50
            mss_15m_pass = (s50_price >= s50_15m_ema20) and (s50_rsi15m >= 50.0)
except Exception as e:
    print(f"15M Data Error: {e}")

all_pass = (not es_veto) and (not thb_veto) and breadth_pass and (not delta_veto) and daily_bullish and mss_15m_pass
is_silent = not all_pass

es_icon = "🔴 VETO" if es_veto else "🟢 PASS"
thb_icon = "🔴 VETO" if thb_veto else "🟢 PASS"
breadth_icon = "🟢 PASS" if breadth_pass else "🔴 VETO"
delta_icon = "🔴 VETO" if delta_veto else "🟢 PASS"
trend_icon = "🟢 BULL" if daily_bullish else "🔴 BEAR"
mss_icon = "🟢 TRIGGERED" if mss_15m_pass else "⚪ AWAITING"

lines = []
if notice_header:
    lines.append(notice_header)

title_tag = "🚨 <b>[ACTION REQUIRED: TFEX S50 LONG]</b>" if all_pass else "⚡ <b>[C2C Engine T - Market Scan]</b>"
lines.extend([
    title_tag,
    f"📅 <b>Time:</b> {bkk_time_str} BKK",
    "━━━━━━━━━━━━━━━━━━",
    f"📈 <b>SET50 Index:</b> {s50_price:.2f} pts [{trend_icon}]",
    f"• Daily EMA(20): {s50_daily_ema20:.2f} | 15M EMA(20): {s50_15m_ema20:.2f}",
    f"• 15M MSS Momentum: RSI(14) = {s50_rsi15m:.1f} [{mss_icon}]",
    "━━━━━━━━━━━━━━━━━━",
    "🛡️ <b>INSTITUTIONAL GATE MATRIX:</b>",
    f"• <b>Intermarket (ES=F):</b> {es_pct:+.2f}% [{es_icon}]",
    f"• <b>FX Slope (USD/THB):</b> {thb_chg:+.2f}% [{thb_icon}]",
    f"• <b>Mega-Cap Breadth:</b> {advancing}/5 Advancing [{breadth_icon}]",
    f"• <b>DELTA Decoupler:</b> {delta_chg:+.2f}% [{delta_icon}]",
    "  " + " | ".join(breadth_details),
    "━━━━━━━━━━━━━━━━━━"
])

if all_pass:
    lines.extend([
        "🎯 <b>REGIME STATUS: GREEN (AUTHORIZED)</b>",
        "📋 <b>INSTITUTIONAL BRACKET TICKET:</b>",
        f"• Size: <b>10 Contracts</b> (฿1.0M Baseline)",
        f"• Long Limit: <b>{s50_price:.1f}</b>",
        f"• Hard Stop: <b>{s50_price - 4.5:.1f}</b> (-4.5 pts cap)",
        f"• <b>T1 (Bank 4 cts):</b> <b>{s50_price + 4.5:.1f}</b> (+4.5 pts &gt;&gt; Move stop to BE)",
        f"• <b>T2A (Fixed 3 cts):</b> <b>{s50_price + 12.0:.1f}</b> (+12.0 pts)",
        f"• <b>T2B (Runner 3 cts):</b> Trail 2H High - 6.0 pts"
    ])
else:
    vetos = []
    if es_veto: vetos.append("ES Drop &le; -0.60%")
    if thb_veto: vetos.append("THB Depreciation &gt; +0.30%")
    if not breadth_pass: vetos.append("Breadth &lt; 3/5")
    if delta_veto: vetos.append("DELTA Drag &lt; -1.50%")
    if not daily_bullish: vetos.append("SET50 &lt; Daily EMA(20)")
    if not mss_15m_pass: vetos.append("15M MSS Structure Not Aligned")
    lines.extend([
        "⚠️ <b>REGIME STATUS: FILTER VETO ACTIVE</b>",
        f"• <b>Suppressed by:</b> {', '.join(vetos)}",
        "• <b>Action:</b> Stand down. Long execution suppressed. <i>(Silent notification)</i>"
    ])

lines.append("━━━━━━━━━━━━━━━━━━\n<i>Automated pipeline running via GitHub Cloud.</i>")
full_msg = "\n".join(lines)

# Direct TradingView S501! Chart and InnovestX Deep Link
keyboard = {
    "inline_keyboard": [
        [
            {"text": "📈 S50 Futures Chart", "url": "https://www.tradingview.com/chart/?symbol=TFEX%3AS501!"},
            {"text": "📲 Open InnovestX", "url": "https://play.google.com/store/apps/details?id=com.scbs.easyinvest"}
        ]
    ]
}

if token and chat_id:
    res = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": full_msg,
            "parse_mode": "HTML",
            "disable_notification": is_silent,
            "reply_markup": keyboard
        },
        timeout=15
    )
    print(f"Telegram status: {res.status_code}")
else:
    print("Missing Telegram credentials.")
