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
    notice_header = "⏸️ <b>[SET CLOSED — PRIOR SESSION AUDIT]</b>"

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
es_pct = 0.0
c_es = safe_get_series("ES=F", "5d", "1d")
if len(c_es) >= 2:
    es_pct = float(((c_es[-1] - c_es[-2]) / c_es[-2]) * 100.0)

long_es_pass = (es_pct > -0.60)
short_es_pass = (es_pct < 0.60)

# 2. USD/THB FX Gate (5-Day Trend Slope)
thb_chg = 0.0
c_thb = safe_get_series("THB=X", "10d", "1d")
if len(c_thb) >= 5:
    thb_chg = float(((c_thb[-1] - c_thb[-5]) / c_thb[-5]) * 100.0)

long_thb_pass = (thb_chg <= 0.30)
short_thb_pass = True

# 3. Breadth & DELTA Decoupler
tickers = ["DELTA.BK", "PTT.BK", "PTTEP.BK", "BDMS.BK", "CPALL.BK"]
advancing = 0
declining = 0
breadth_items = []
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
            breadth_items.append(f"🟢 {clean} {chg:+.2f}%")
        elif chg < 0:
            declining += 1
            breadth_items.append(f"🔴 {clean} {chg:+.2f}%")
        else:
            breadth_items.append(f"⚪ {clean} 0.00%")
    else:
        breadth_items.append(f"⚪ {clean} Flat")

long_breadth_pass = (advancing >= 3)
short_breadth_pass = (declining >= 3)
long_delta_pass = (delta_chg >= -1.50)
short_delta_pass = (delta_chg <= 1.50)

# 4. Multi-Timeframe Structure & Playbook Layer Calculations
s50_price = 930.0
s50_daily_ema20 = 925.0
s50_15m_ema20 = 928.0
s50_rsi15m = 50.0
s50_atr = 4.5
daily_adx = 22.0
plus_di = 25.0
minus_di = 20.0
daily_bullish = True
daily_bearish = False
mss_15m_long = False
mss_15m_short = False
prior_vah = 928.0
prior_val = 920.0

# Layer A: Daily Trend, ATR, & ADX Filter
try:
    df_d = yf.download("^SET50", period="6mo", interval="1d", progress=False)
    if df_d is not None and not df_d.empty:
        c_series = df_d["Close"].iloc[:, 0] if isinstance(df_d.columns, pd.MultiIndex) else df_d["Close"]
        h_series = df_d["High"].iloc[:, 0] if isinstance(df_d.columns, pd.MultiIndex) else df_d["High"]
        l_series = df_d["Low"].iloc[:, 0] if isinstance(df_d.columns, pd.MultiIndex) else df_d["Low"]

        c_vals = c_series.dropna().values
        h_vals = h_series.dropna().values
        l_vals = l_series.dropna().values

        if len(c_vals) >= 30:
            s50_price = float(c_vals[-1])
            ema_series = pd.Series(c_vals).ewm(span=20, adjust=False).mean()
            s50_daily_ema20 = float(ema_series.iloc[-1])
            daily_bullish = s50_price >= s50_daily_ema20
            daily_bearish = s50_price < s50_daily_ema20

            if len(h_vals) >= 2:
                prev_h = h_vals[-2]
                prev_l = l_vals[-2]
                prev_rng = prev_h - prev_l
                prior_vah = prev_h - (prev_rng * 0.15)
                prior_val = prev_l + (prev_rng * 0.15)

            tr_list = []
            for k in range(1, len(c_vals)):
                tr_val = max(h_vals[k] - l_vals[k], abs(h_vals[k] - c_vals[k-1]), abs(l_vals[k] - c_vals[k-1]))
                tr_list.append(tr_val)
            if len(tr_list) >= 14:
                s50_atr = max(float(np.mean(tr_list[-14:])), 3.5)

            df_adx = pd.DataFrame({"High": h_vals, "Low": l_vals, "Close": c_vals})
            up = df_adx["High"].diff()
            down = -df_adx["Low"].diff()
            p_dm = np.where((up > down) & (up > 0), up, 0.0)
            m_dm = np.where((down > up) & (down > 0), down, 0.0)
            tr_s = pd.Series(tr_list)
            p_di_s = 100.0 * (pd.Series(p_dm[1:]).rolling(14).sum() / (tr_s.rolling(14).sum() + 1e-9))
            m_di_s = 100.0 * (pd.Series(m_dm[1:]).rolling(14).sum() / (tr_s.rolling(14).sum() + 1e-9))
            dx_s = 100.0 * ((p_di_s - m_di_s).abs() / ((p_di_s + m_di_s).abs() + 1e-9))
            adx_val = dx_s.rolling(14).mean().iloc[-1]
            if not np.isnan(adx_val):
                daily_adx = float(adx_val)
                plus_di = float(p_di_s.iloc[-1])
                minus_di = float(m_di_s.iloc[-1])
except Exception as e:
    print(f"Daily/ADX calculation error: {e}")

# Layer B & C: 15M MSS Trigger & Zone of Interest (ZOI)
try:
    df_15m = yf.download("^SET50", period="5d", interval="15m", progress=False)
    if df_15m is not None and not df_15m.empty:
        c_15m = df_15m["Close"].iloc[:, 0].dropna() if isinstance(df_15m.columns, pd.MultiIndex) else df_15m["Close"].dropna()
        if len(c_15m) >= 25:
            s50_price = float(c_15m.iloc[-1])
            ema20_15m_series = c_15m.ewm(span=20, adjust=False).mean()
            s50_15m_ema20 = float(ema20_15m_series.iloc[-1])

            delta_p = c_15m.diff()
            gain = (delta_p.where(delta_p > 0, 0)).rolling(14).mean()
            loss = (-delta_p.where(delta_p < 0, 0)).rolling(14).mean()
            rs = gain / (loss + 1e-9)
            s50_rsi15m = float((100 - (100 / (1 + rs))).iloc[-1])

            mss_15m_long = (s50_price >= s50_15m_ema20) and (s50_rsi15m >= 50.0)
            mss_15m_short = (s50_price <= s50_15m_ema20) and (s50_rsi15m <= 48.0)
except Exception as e:
    print(f"15M calculation error: {e}")

# Layer D: Futures Basis Clamping
basis_delta = 0.5
try:
    c_fut = safe_get_series("0840.HK", "5d", "1d")
    if len(c_fut) >= 2:
        diff = float(c_fut[-1] - s50_price)
        if abs(diff) < 25.0:
            basis_delta = diff
except Exception:
    basis_delta = 0.5

basis_pass_long = (basis_delta < 5.5)
basis_pass_short = (basis_delta > -3.5)

va_pass_long = (s50_price >= prior_vah)
va_pass_short = (s50_price <= prior_val)
zoi_pass = abs(s50_price - s50_daily_ema20) <= (s50_atr * 0.75)
adx_pass = (daily_adx >= 20.0)

is_long_authorized = (
    long_es_pass and long_thb_pass and long_breadth_pass and long_delta_pass and
    daily_bullish and mss_15m_long and basis_pass_long and adx_pass and
    (plus_di > minus_di) and va_pass_long and zoi_pass
)

is_short_authorized = (
    short_es_pass and short_thb_pass and short_breadth_pass and short_delta_pass and
    daily_bearish and mss_15m_short and basis_pass_short and adx_pass and
    (minus_di > plus_di) and va_pass_short and zoi_pass
)

has_actionable_signal = is_long_authorized or is_short_authorized
is_silent = not has_actionable_signal

trend_icon = "🟢 BULL" if daily_bullish else "🔴 BEAR"
adx_icon = "🟢 TRENDING" if daily_adx >= 20.0 else "🟡 CHOP/FLAT"
rsi_icon = "🟢 BULL MSS" if mss_15m_long else ("🔴 BEAR MSS" if mss_15m_short else "FLAT")
basis_tag = "🟢 FAIR" if (-3.5 < basis_delta < 5.5) else ("🔴 HIGH PREMIUM" if basis_delta >= 5.5 else "🔴 DEEP DISCOUNT")

es_tag = "🟢 CLEAR" if (long_es_pass if daily_bullish else short_es_pass) else "🔴 OPPOSING"
thb_tag = "🟢 CLEAR" if long_thb_pass else "🔴 RAPID DEPREC"
breadth_tag = "🟢 PASS" if (long_breadth_pass if daily_bullish else short_breadth_pass) else "🟢 NEUTRAL"
delta_tag = "🔴 DRAG VETO" if delta_chg < -1.50 else ("🔴 PUMP VETO" if delta_chg > 1.50 and daily_bearish else "🟢 PASS")

lines = []
if notice_header:
    lines.append(notice_header)

title_tag = "🚨 <b>[ACTION REQUIRED: TFEX S50 LONG]</b>" if is_long_authorized else (
    "🚨 <b>[ACTION REQUIRED: TFEX S50 SHORT]</b>" if is_short_authorized else
    "⚡ <b>C2C ENGINE T // S50 INTELLIGENCE</b>"
)

# Clean formatted breadth tree
b_row1 = f"  ├ {breadth_items[0]}  {breadth_items[1]}" if len(breadth_items) >= 2 else ""
b_row2 = f"  ├ {breadth_items[2]}  {breadth_items[3]}" if len(breadth_items) >= 4 else ""
b_row3 = f"  └ {breadth_items[4]}" if len(breadth_items) >= 5 else ""

lines.extend([
    title_tag,
    f"📅 <code>{bkk_time_str} BKK</code>",
    "━━━━━━━━━━━━━━━━━━",
    f"📈 <b>S50 CASH INDEX:</b> <b>{s50_price:.2f} pts</b> [{trend_icon} REGIME]",
    f"• Structural 20-EMA: <b>{s50_daily_ema20:.2f}</b> | Fast 15M: <b>{s50_15m_ema20:.2f}</b>",
    f"• Trend Strength: ADX <b>{daily_adx:.1f}</b> [{adx_icon}] | RSI <b>{s50_rsi15m:.1f}</b> [{rsi_icon}]",
    f"• Auction Profile: VAH <b>{prior_vah:.1f}</b> / VAL <b>{prior_val:.1f}</b> | Basis: <b>{basis_delta:+.1f} pts</b> [{basis_tag}]",
    "━━━━━━━━━━━━━━━━━━",
    "🛡️ <b>INSTITUTIONAL GATE CHECK:</b>",
    f"• Asian Delta (ES=F): <b>{es_pct:+.2f}%</b> [{es_tag}]",
    f"• FX Trend (USD/THB): <b>{thb_chg:+.2f}%</b> [{thb_tag}]",
    f"• Mega-Cap Breadth: <b>{advancing} Adv / {declining} Dec</b> [{breadth_tag}]",
    f"• DELTA Decoupler: <b>{delta_chg:+.2f}%</b> [{delta_tag}]",
    b_row1,
    b_row2,
    b_row3,
    "━━━━━━━━━━━━━━━━━━"
])

long_t1 = round(s50_price + max(5.5, s50_atr * 1.25), 1)
short_t1 = round(s50_price - 4.5, 1)
long_t2a = round(s50_price + 12.0, 1)
short_t2a = round(s50_price - 14.0, 1)
chandelier_long = round(s50_price - (1.5 * s50_atr), 1)
chandelier_short = round(s50_price + (1.5 * s50_atr), 1)
stop_distance = round(min(s50_atr * 0.75, 4.5), 1)

if is_long_authorized:
    lines.extend([
        "🎯 <b>REGIME STATUS: GREEN (LONG AUTHORIZED)</b>",
        "📋 <b>INSTITUTIONAL MASTER TICKET (10 Contracts):</b>",
        f"• Order: <b>BUY OPEN 10 Contracts</b> (฿1.0M Baseline)",
        f"• Limit Entry: <b>{s50_price:.1f}</b> (In ZOI)",
        f"• Hard Stop: <b>{s50_price - stop_distance:.1f}</b> (-{stop_distance} pts structural cap)",
        f"• <b>Tranche 1 (Bank 4 cts):</b> Limit <b>{long_t1:.1f}</b> (+{long_t1-s50_price:.1f} pts &gt;&gt; Move stop to BE)",
        f"• <b>Tranche 2A (Fixed 3 cts):</b> Limit <b>{long_t2a:.1f}</b> (+12.0 pts)",
        f"• <b>Tranche 2B (Runner 3 cts):</b> Chandelier Trail &gt;&gt; High - {1.5*s50_atr:.1f} pts (Initial: <b>{chandelier_long:.1f}</b>)",
        "━━━━━━━━━━━━━━━━━━",
        "🔥 <b>HOUSE MONEY PYRAMID TICKET (+2 Contracts):</b>",
        f"• Trigger: After T1 banks (+{long_t1-s50_price:.1f} pts), place <b>BUY OPEN +2 cts</b> on next 15M breakout.",
        f"• Anchor Stop: <b>{s50_price:.1f}</b> (Breakeven). 100% financed by T1 banked profit.",
        "━━━━━━━━━━━━━━━━━━",
        "📲 <b>QUICK ORDER:</b>",
        f"<code>BUY OPEN S50 10 cts @ {s50_price:.1f} | Stop: {s50_price - stop_distance:.1f}</code>"
    ])
elif is_short_authorized:
    lines.extend([
        "🎯 <b>REGIME STATUS: RED (SHORT AUTHORIZED)</b>",
        "📋 <b>INSTITUTIONAL MASTER TICKET (10 Contracts):</b>",
        f"• Order: <b>SELL OPEN (SHORT) 10 Contracts</b>
