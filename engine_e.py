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
event_name = os.environ.get("EVENT_NAME", "workflow_dispatch")
trade_outcome = os.environ.get("INPUT_OUTCOME", "none")

bkk_now = datetime.now(pytz.timezone("Asia/Bangkok"))
ny_now = datetime.now(pytz.timezone("America/New_York"))
ny_date_str = ny_now.strftime("%Y-%m-%d")
bkk_time_str = bkk_now.strftime("%Y-%m-%d %H:%M:%S")
is_friday = (bkk_now.weekday() == 4)

# 1. State Maintenance
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

# 2. Market Calendar Check
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

# 3. Macro Feeds
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

# 4. Regime Classification (QQQ)
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
            regime_name = "Regime 4: Structural Bear"
            regime_icon = "🔴"
            r_thb = 0.0
            r_label = "0.0R (100% Cash Gate)"
            vault_a_alloc = "100% SGOV (0% Equities)"
            risk_authorized = False
        elif p_now > p_ema20 > p_ema50 > p_sma200:
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
            regime_name = "Regime 2: Bull Pullback"
            regime_icon = "🟠"
            r_thb = 15000.0 if vix_elevated else 22500.0
            r_label = "0.50R (฿15,000 THB) [VIX High]" if vix_elevated else "0.75R (฿22,500 THB)"
            vault_a_alloc = "35% QQQ / 25% SMH / 40% SGOV"
        else:
            regime_name = "Regime 3: Rangebound Chop"
            regime_icon = "🟡"
            r_thb = 15000.0
            r_label = "0.50R (฿15,000 THB)"
            vault_a_alloc = "35% QQQ / 25% SMH / 40% SGOV"
except Exception as e:
    print(f"Macro error: {e}")

if circuit_breaker_active:
    r_thb = 0.0
    r_label = f"0.0R (HALTED: {circuit_reason})"
    risk_authorized = False

# 5. Universe Definition
watchpool = [
    {"ticker": "NVDA",  "dr": "NVDA80X",   "ratio": 200,   "exchange": "NASDAQ", "tier": "HIGH_BETA", "bench": "SMH", "is_etf": False},
    {"ticker": "PLTR",  "dr": "PLTR01",    "ratio": 1100,  "exchange": "NASDAQ", "tier": "HIGH_BETA", "bench": "XLK", "is_etf": False},
    {"ticker": "TSLA",  "dr": "TSLA80X",   "ratio": 5000,  "exchange": "NASDAQ", "tier": "HIGH_BETA", "bench": "QQQ", "is_etf": False},
    {"ticker": "AMD",   "dr": "AMD80X",    "ratio": 5000,  "exchange": "NASDAQ", "tier": "HIGH_BETA", "bench": "SMH", "is_etf": False},
    {"ticker": "COIN",  "dr": "COIN01",    "ratio": 2000,  "exchange": "NASDAQ", "tier": "HIGH_BETA", "bench": "QQQ", "is_etf": False},
    {"ticker": "ALAB",  "dr": "ALAB01",    "ratio": 1000,  "exchange": "NASDAQ", "tier": "HIGH_BETA", "bench": "SMH", "is_etf": False},
    {"ticker": "MU",    "dr": "MICRON01",  "ratio": 1000,  "exchange": "NASDAQ", "tier": "HIGH_BETA", "bench": "SMH", "is_etf": False},
    {"ticker": "SOXX",  "dr": "SOXX23",    "ratio": 100,   "exchange": "NASDAQ", "tier": "HIGH_BETA", "bench": "SMH", "is_etf": True},
    {"ticker": "AAPL",  "dr": "AAPL80X",   "ratio": 1000,  "exchange": "NASDAQ", "tier": "MEGA_CORE", "bench": "XLK", "is_etf": False},
    {"ticker": "MSFT",  "dr": "MSFT80X",   "ratio": 1000,  "exchange": "NASDAQ", "tier": "MEGA_CORE", "bench": "XLK", "is_etf": False},
    {"ticker": "GOOGL", "dr": "GOOG80X",   "ratio": 500,   "exchange": "NASDAQ", "tier": "MEGA_CORE", "bench": "QQQ", "is_etf": False},
    {"ticker": "META",  "dr": "META80X",   "ratio": 1000,  "exchange": "NASDAQ", "tier": "MEGA_CORE", "bench": "QQQ", "is_etf": False},
    {"ticker": "AMZN",  "dr": "AMZN80X",   "ratio": 500,   "exchange": "NASDAQ", "tier": "MEGA_CORE", "bench": "QQQ", "is_etf": False},
    {"ticker": "LLY",   "dr": "LLY80X",    "ratio": 1000,  "exchange": "NYSE",   "tier": "MEGA_CORE", "bench": "QQQ", "is_etf": False},
    {"ticker": "ISRG",  "dr": "ISRG80",    "ratio": 1000,  "exchange": "NASDAQ", "tier": "MEGA_CORE", "bench": "QQQ", "is_etf": False},
    {"ticker": "QQQ",   "dr": "QQQ80X",    "ratio": 1000,  "exchange": "NASDAQ", "tier": "MEGA_CORE", "bench": "QQQ", "is_etf": True}
]

candidates = []
summary_lines = []

for item in watchpool:
    sym = item["ticker"]
    dr_sym = item["dr"]
    ratio = item["ratio"]
    exchange = item["exchange"]
    tier = item["tier"]
    bench_sym = item["bench"]
    is_etf = item.get("is_etf", False)

    try:
        t_obj = yf.Ticker(sym)
        hist = t_obj.history(period="3mo", interval="1d")
        c = safe_series(hist, "Close")
        lows = safe_series(hist, "Low")
        vol = safe_series(hist, "Volume")
        if len(c) < 25:
            continue

        curr_p = float(c[-1])
        s_c = pd.Series(c)
        ema21 = float(s_c.ewm(span=21, adjust=False).mean().iloc[-1])

        b_3d, b_10d = bench_returns.get(bench_sym, (qqq_3d, qqq_10d))
        p_3d = float(c[-4]) if len(c) >= 4 else curr_p
        p_10d = float(c[-11]) if len(c) >= 11 else curr_p
        ret_3d = ((curr_p - p_3d) / p_3d) * 100.0
        ret_10d = ((curr_p - p_10d) / p_10d) * 100.0

        rs_delta_3d = ret_3d - b_3d
        rs_delta_10d = ret_10d - b_10d
        composite_rs = (0.60 * rs_delta_3d) + (0.40 * rs_delta_10d)

        sma20 = s_c.rolling(20).mean()
        std20 = s_c.rolling(20).std()
        bbw = ((sma20 + 2*std20) - (sma20 - 2*std20)) / sma20 * 100.0
        is_compressed = bool(bbw.iloc[-1] <= bbw.tail(60).quantile(0.25))

        vdu_pass = True
        if len(vol) >= 20:
            avg_vol20 = np.mean(vol[-20:])
            vdu_pass = (vol[-1] <= avg_vol20 * 0.85)

        imminent_earn = False
        if not is_etf:
            try:
                cal = t_obj.calendar
                if cal is not None and not (isinstance(cal, pd.DataFrame) and cal.empty):
                    dt = None
                    if isinstance(cal, pd.DataFrame) and "Earnings Date" in cal.index:
                        dt = pd.to_datetime(cal.loc["Earnings Date"].iloc[0])
                    elif isinstance(cal, dict) and "Earnings Date" in cal:
                        dt = pd.to_datetime(cal["Earnings Date"][0])
                    if dt:
                        dt_naive = dt.tz_localize(None) if dt.tzinfo else dt
                        days_diff = (dt_naive - datetime.now()).days
                        if 0 <= days_diff <= 5:
                            imminent_earn = True
            except Exception:
                pass

        dist_21 = ((curr_p - ema21) / ema21) * 100.0
        comp_tags = []
        if is_compressed: comp_tags.append("📦 Sqz")
        if vdu_pass: comp_tags.append("💧 VDU")
        tag_str = f"[{' '.join(comp_tags)}]" if comp_tags else "[Normal]"
        earn_tag = " ⚠️ EARN" if imminent_earn else ""

        summary_lines.append(
            f"• <b>{dr_sym}</b> (${curr_p:.2f}): RS vs {bench_sym} {composite_rs:+.1f}% | 21EMA {dist_21:+.1f}% | {tag_str}{earn_tag}"
        )

        recent_low = float(np.min(lows[-3:])) if len(lows) >= 3 else curr_p * 0.975
        structural_stop = max(recent_low * 0.998, curr_p * 0.975)

        # Anti-Chase Envelope: Must be between -1.0% and +2.0% of 21 EMA
        within_ema_envelope = (-1.0 <= dist_21 <= 2.0)
        base_quality_ok = is_compressed or vdu_pass

        if within_ema_envelope and base_quality_ok and (not imminent_earn) and (composite_rs > 0) and risk_authorized and not vix_elevated:
            candidates.append({
                "dr": dr_sym,
                "ticker": sym,
                "tier": tier,
                "bench": bench_sym,
                "exchange": exchange,
                "usd_price": curr_p,
                "ratio": ratio,
                "rs": composite_rs,
                "dist_21": dist_21,
                "stop_usd": structural_stop
            })
    except Exception as e:
        print(f"Error {sym}: {e}")

candidates.sort(key=lambda x: x["rs"], reverse=True)
top_picks = candidates[:2]

ticket_blocks = []
settrade_copy_blocks = []
has_executable_trade = False

if top_picks and risk_authorized and r_thb > 0:
    has_executable_trade = True
    ticket_blocks.append("📋 <b>OPTIMIZED ALPHA TICKETS (THB Sized):</b>")
    for p in top_picks:
        entry_usd = p["usd_price"]
        stop_usd = p["stop_usd"]
        ratio = p["ratio"]
        tier = p["tier"]

        entry_thb = (entry_usd * usd_thb) / ratio
        stop_thb = (stop_usd * usd_thb) / ratio
        risk_per_unit = entry_thb - stop_thb

        if risk_per_unit <= 0.001:
            continue

        raw_units = int(r_thb / risk_per_unit)
        raw_outlay = raw_units * entry_thb
        max_cap = 600000.0
        if raw_outlay > max_cap:
            units = int(max_cap / entry_thb)
            capped_tag = " <i>(Capped at 20% Vault B)</i>"
        else:
            units = raw_units
            capped_tag = ""

        if tier == "HIGH_BETA":
            t1_multiple = 2.0
            t1_pct = 0.50
            t1_tag = "+2.0R Volatility Target"
        else:
            t1_multiple = 1.5
            t1_pct = 0.60
            t1_tag = "+1.5R Core Bank"

        target1_thb = entry_thb + (t1_multiple * risk_per_unit)
        t1_units = int(units * t1_pct)
        t2_units = units - t1_units
        stop_pct = ((stop_usd - entry_usd) / entry_usd) * 100.0

        ticket_card = (
            f"🎯 <b>{p['dr']} ({p['ticker']}) - [{tier}] (vs {p['bench']}):</b>\n"
            f"  • Entry: <b>฿{entry_thb:.2f}</b> (${entry_usd:.2f}) [21EMA: {p['dist_21']:+.1f}%]\n"
            f"  • Hard Stop: <b>฿{stop_thb:.2f}</b> ({stop_pct:.1f}% pivot)\n"
            f"  • Sizing: <b>{units:,} units</b> (Outlay: ฿{units*entry_thb:,.0f} THB){capped_tag}\n"
            f"  • <b>T1 ({int(t1_pct*100)}% / {t1_units:,} u):</b> Limit <b>฿{target1_thb:.2f}</b> ({t1_tag} &gt;&gt; Move stop to BE)\n"
            f"  • <b>T2 Runner ({int((1-t1_pct)*100)}% / {t2_units:,} u):</b> Trail Daily Close &ge; 21 EMA\n"
            f"  • <b>⚡ Free-Roll Pyramiding:</b> On T1 hit, add <b>+{int(units*0.30):,} u</b> on next 15M breakout base (Stop @ BE)\n"
            f"  • <b>🛡️ 4-Candle Stall Rule:</b> If 4x 15M candles stall without new highs, pull stop to <b>-0.5R (฿{entry_thb - (0.5*risk_per_unit):.2f})</b>\n"
            f"  • <b>🛡️ LP Spread Guard:</b> Max Buy Limit = Offer + 1 tick. If Bid/Offer spread &gt; 0.4%, DO NOT CHASE.\n"
            f"  • <b>Time-Stop:</b> Flatten if T1 unhit in 72 hours (3 sessions)\n"
        )
        ticket_blocks.append(ticket_card)

        settrade_copy_blocks.append(
            f"<code>BUY {p['dr']} @ ฿{entry_thb:.2f} | Vol: {units:,} | Stop: ฿{stop_thb:.2f}</code>"
        )
elif circuit_breaker_active:
    ticket_blocks.append(f"⛔ <b>CIRCUIT BREAKER TRIGGERED:</b>\n• {circuit_reason}\n• All new buy orders suppressed.")
elif vix_elevated:
    ticket_blocks.append(f"⚠️ <b>VOLATILITY GATE ACTIVE:</b>\n• VIX at {vix_val:.1f} (&gt; 28.0). High-beta breakouts vetoed.")
else:
    ticket_blocks.append("📋 <b>EXECUTION:</b> Zero breakout setups qualified in launchpad envelope. Retain cash in SCB EZ / MMF.")

lines = []
if notice_header:
    lines.append(notice_header)

title_tag = "🚨 <b>[ACTION REQUIRED: US DRx ENTRY]</b>" if has_executable_trade else "🇺🇸 <b>[C2C Engine E - Dual-Vault Briefing]</b>"
vix_icon = "🟢" if vix_val < 20 else ("🟡" if vix_val <= 28 else "🔴 HIGH")

lines.extend([
    title_tag,
    f"📅 <b>Time:</b> {bkk_time_str} BKK",
    f"💵 <b>USD/THB:</b> ฿{usd_thb:.2f} | <b>VIX:</b> {vix_val:.1f} [{vix_icon}]",
    "━━━━━━━━━━━━━━━━━━",
    f"📊 <b>MACRO REGIME:</b> {regime_icon} <b>{regime_name}</b>",
    f"• <b>Risk Budget:</b> <b>{r_label}</b>",
    f"• <b>Vault A Target:</b> <code>{vault_a_alloc}</code>",
    f"• <b>Circuit Status:</b> Streak: <b>{win_streak}W</b> | Daily Stops: <b>{state.get('daily_stopouts', 0)}/2</b>",
    "━━━━━━━━━━━━━━━━━━",
    "🔍 <b>WATCHPOOL METRICS (Sector-Benchmarked RS):</b>",
    "\n".join(summary_lines) if summary_lines else "• Data pending",
    "━━━━━━━━━━━━━━━━━━"
])
lines.extend(ticket_blocks)

if is_friday:
    lines.extend([
        "━━━━━━━━━━━━━━━━━━",
        "🛡️ <b>FRIDAY OVERNIGHT COLLAR DIRECTIVE:</b>",
        "• If T1 is not banked before market close, tighten stop to BE or flatten to eliminate unhedged weekend gap risk.",
        "• If Net Long Exposure &gt; ฿1.5M and VIX &gt; 22, park hedge tranche in SGOV."
    ])

if settrade_copy_blocks:
    lines.append("━━━━━━━━━━━━━━━━━━\n📲 <b>SETTRADE QUICK-PASTE:</b>")
    lines.extend(settrade_copy_blocks)

lines.append("━━━━━━━━━━━━━━━━━━\n<i>Automated pipeline running via GitHub Cloud.</i>")
full_msg = "\n".join(lines)

keyboard_buttons = [
    [
        {"text": "📲 Open InnovestX", "url": "https://play.google.com/store/apps/details?id=com.scbs.easyinvest"}
    ]
]

if top_picks:
    for p in top_picks:
        sym = p["ticker"]
        exch = p.get("exchange", "NASDAQ")
        tv_chart_url = f"https://www.tradingview.com/chart/?symbol={exch}%3A{sym}"
        keyboard_buttons.append([
            {"text": f"📈 {sym} Chart", "url": tv_chart_url}
        ])
else:
    keyboard_buttons.append([
        {"text": "📈 QQQ Chart", "url": "https://www.tradingview.com/chart/?symbol=NASDAQ%3AQQQ"}
    ])

keyboard = {"inline_keyboard": keyboard_buttons}

print(f"Message length: {len(full_msg)} chars")
if token and chat_id:
    res = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": full_msg,
            "parse_mode": "HTML",
            "disable_notification": False,
            "reply_markup": keyboard
        },
        timeout=20
    )
    print(f"Telegram status: {res.status_code}")
    if res.status_code != 200:
        print(f"Telegram error response: {res.text}")
        clean_plain = (
            full_msg.replace("<b>", "")
            .replace("</b>", "")
            .replace("<code>", "")
            .replace("</code>", "")
            .replace("<i>", "")
            .replace("</i>", "")
            .replace("&gt;", ">")
            .replace("&lt;", "<")
        )
        res_fb = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": clean_plain,
                "disable_notification": False,
                "reply_markup": keyboard
            },
            timeout=20
        )
        print(f"Fallback plain text status: {res_fb.status_code}")
else:
    print(f"Credentials missing. token={bool(token)}, chat_id={bool(chat_id)}")
