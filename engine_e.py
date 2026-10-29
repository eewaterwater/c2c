import os
import sys
import json
import html
import requests
from datetime import datetime, timezone
import pytz
import pandas as pd
import numpy as np
import yfinance as yf

token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("TELEGRAM_CHAT_ID")
event_name = os.environ.get("EVENT_NAME", "workflow_dispatch")
trade_outcome = os.environ.get("INPUT_OUTCOME", "none")

bkk_now = datetime.now(pytz.timezone("Asia/Bangkok"))
ny_now = datetime.now(pytz.timezone("America/New_York"))
ny_date_str = ny_now.strftime("%Y-%m-%d")
bkk_time_str = bkk_now.strftime("%Y-%m-%d %H:%M:%S")
is_friday = (bkk_now.weekday() == 4)

AVAILABLE_CASH_THB = 113000.0
MEGA_CAP_EARNINGS_WATCHLIST = ["MSFT", "AAPL", "NVDA", "AMZN", "GOOGL", "META"]
INDEX_PROXIES = ["QQQ", "SPY", "SOXX", "SMH", "XLK"]

# ==============================================================================
# 1. STATE MACHINE MANAGEMENT
# ==============================================================================
state_path = "state.json"
state = {
    "consecutive_wins": 0,
    "daily_stopouts": 0,
    "weekly_stopouts": 0,
    "last_trade_date": ny_date_str,
    "active_position": None
}

if os.path.exists(state_path):
    try:
        with open(state_path, "r") as f:
            state = json.load(f)
    except Exception as e:
        print(f"Warning reading state.json: {e}")

if state.get("last_trade_date") != ny_date_str:
    state["daily_stopouts"] = 0
    state["last_trade_date"] = ny_date_str

if trade_outcome == "win":
    state["consecutive_wins"] = state.get("consecutive_wins", 0) + 1
    state["daily_stopouts"] = 0
elif trade_outcome == "stopout":
    state["consecutive_wins"] = 0
    state["daily_stopouts"] = state.get("daily_stopouts", 0) + 1
    state["weekly_stopouts"] = state.get("weekly_stopouts", 0) + 1
elif trade_outcome == "reset":
    state["consecutive_wins"] = 0
    state["daily_stopouts"] = 0
    state["weekly_stopouts"] = 0

try:
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)
except Exception as e:
    print(f"Warning saving state.json: {e}")

circuit_breaker_active = False
circuit_reason = ""
if state.get("daily_stopouts", 0) >= 2:
    circuit_breaker_active = True
    circuit_reason = "Daily Stop-Out Limit Reached (2 stops)"
elif state.get("weekly_stopouts", 0) >= 3:
    circuit_breaker_active = True
    circuit_reason = "Weekly Stop-Out Limit Reached (3 stops in rolling week)"

win_streak = state.get("consecutive_wins", 0)
streak_boost = win_streak >= 3

# ==============================================================================
# 2. MARKET CALENDAR CHECK
# ==============================================================================
is_us_open_today = True
try:
    import exchange_calendars as xcals
    xnys = xcals.get_calendar("XNYS")
    is_us_open_today = bool(xnys.is_session(ny_date_str))
except Exception:
    is_us_open_today = (ny_now.weekday() < 5)

notice_header = ""
if not is_us_open_today:
    if event_name == "schedule":
        print("Market closed & schedule trigger. Standing down cleanly.")
        sys.exit(0)
    else:
        notice_header = "⚠️ <b>[TEST RUN: US Market Closed - Using Prior Close Data]</b>\n"

def safe_series(df, col="Close"):
    if df is None or df.empty:
        return np.array([])
    try:
        if isinstance(df.columns, pd.MultiIndex):
            return df[col].iloc[:, 0].dropna().values
        return df[col].dropna().values
    except Exception:
        return np.array([])

# ==============================================================================
# 3. MACRO FEEDS & BENCHMARKS
# ==============================================================================
usd_thb = 35.0
try:
    thb_df = yf.download("THB=X", period="5d", interval="1d", progress=False)
    c_thb = safe_series(thb_df, "Close")
    if len(c_thb) > 0:
        usd_thb = float(c_thb[-1])
except Exception:
    pass

vix_val = 18.0
try:
    vix_df = yf.download("^VIX", period="5d", interval="1d", progress=False)
    c_vix = safe_series(vix_df, "Close")
    if len(c_vix) > 0:
        vix_val = float(c_vix[-1])
except Exception:
    pass

vix_elevated = vix_val > 28.0

def calc_benchmark_returns(sym):
    try:
        df = yf.download(sym, period="1mo", interval="1d", progress=False)
        c = safe_series(df, "Close")
        if len(c) >= 11:
            ret_3d = ((float(c[-1]) - float(c[-4])) / float(c[-4])) * 100.0
            ret_10d = ((float(c[-1]) - float(c[-11])) / float(c[-11])) * 100.0
            return ret_3d, ret_10d
    except Exception:
        pass
    return 0.0, 0.0

qqq_3d, qqq_10d = calc_benchmark_returns("QQQ")
smh_3d, smh_10d = calc_benchmark_returns("SMH")
xlk_3d, xlk_10d = calc_benchmark_returns("XLK")

bench_returns = {
    "QQQ": (qqq_3d, qqq_10d),
    "SMH": (smh_3d, smh_10d),
    "XLK": (xlk_3d, xlk_10d)
}

# ==============================================================================
# 4. 4-REGIME CLASSIFICATION
# ==============================================================================
regime_id = 3
regime_name = "Regime 3: Rangebound Chop"
regime_icon = "🟡"
r_thb = 15000.0
r_label = "0.50R (฿15,000 THB)"
vault_a_alloc = "35% QQQ / 25% SMH / 40% SGOV"
risk_authorized = not circuit_breaker_active

try:
    qqq_df = yf.download("QQQ", period="1y", interval="1d", progress=False)
    c_qqq = safe_series(qqq_df, "Close")
    if len(c_qqq) >= 200:
        p_now = float(c_qqq[-1])
        s_qqq = pd.Series(c_qqq)
        p_ema20 = float(s_qqq.ewm(span=20, adjust=False).mean().iloc[-1])
        p_ema50 = float(s_qqq.ewm(span=50, adjust=False).mean().iloc[-1])
        sma200_series = s_qqq.rolling(200).mean()
        p_sma200 = float(sma200_series.iloc[-1])

        is_bear_confirmed = False
        if len(c_qqq) >= 3 and len(sma200_series) >= 3:
            recent_closes = c_qqq[-3:]
            recent_smas = sma200_series.values[-3:]
            is_bear_confirmed = all(c < (sma * 0.985) for c, sma in zip(recent_closes, recent_smas))

        if is_bear_confirmed:
            regime_id = 4
            regime_name = "Regime 4: Structural Bear"
            regime_icon = "🔴"
            r_thb = 0.0
            r_label = "0.0R (100% Cash Gate)"
            vault_a_alloc = "100% SGOV (0% Equities)"
            risk_authorized = False
        elif p_now > p_ema20 > p_ema50 > p_sma200:
            regime_id = 1
            regime_name = "Regime 1: Confirmed Bull"
            regime_icon = "🟢"
            if streak_boost and not vix_elevated:
                r_thb = 45000.0
                r_label = f"1.50R (฿45,000 THB) 🔥 [Streak: {win_streak}W]"
            elif vix_elevated:
                r_thb = 15000.0
                r_label = f"0.50R (฿15,000 THB) [VIX Spike ({vix_val:.1f})]"
            else:
                r_thb = 30000.0
                r_label = "1.00R (฿30,000 THB)"
            vault_a_alloc = "55% QQQ / 35% SMH / 10% SGOV"
        elif p_now < p_ema20 and p_now > p_sma200:
            regime_id = 2
            regime_name = "Regime 2: Bull Pullback"
            regime_icon = "🟠"
            r_thb = 15000.0 if vix_elevated else 22500.0
            r_label = "0.50R (฿15,000 THB) [VIX High]" if vix_elevated else "0.75R (฿22,500 THB)"
            vault_a_
